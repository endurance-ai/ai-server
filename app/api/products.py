"""Products API.

GET  /v1/products/{id}          — 상품 상세 (crawler DB, 비로그인 허용)
POST /v1/products/{id}/view     — 상품 조회 기록 (24h 중복 제거)
GET  /v1/products/viewed        — 조회한 상품 목록 (세션 기준)
POST /v1/products/{id}/link-check — 상품 URL 유효성 확인
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool
from app.services.curation_taste import record_product_signal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["products"])

# 일부 쇼핑몰은 기본 User-Agent(python-httpx/...)를 봇으로 감지해 403 을 반환한다.
# 브라우저 UA 로 위장해 정상 상품이 dead 로 오판되는 것을 방지한다.
_LINK_CHECK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# UA 하나만으로는 부족한 쇼핑몰(예: Zara/Akamai Bot Manager)을 위해 브라우저가 보내는
# 표준 헤더를 함께 실어 봇 지문을 줄인다.
_LINK_CHECK_HEADERS = {
    "User-Agent": _LINK_CHECK_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Akamai/Cloudflare 등의 봇 차단은 데이터센터(EC2) IP 를 헤더와 무관하게 막을 수 있다.
# 이는 "죽은 링크"가 아니라 "봇 차단"이므로, 실제 사용자의 브라우저에서는 정상 접속된다.
# 따라서 이 상태코드들은 dead 로 오판하지 않고 살아있는 것으로 취급(fail-open)한다.
_ANTIBOT_STATUSES = frozenset({401, 403, 429})


# ── Schemas ───────────────────────────────────────────────────────────────────


class BrandNode(BaseModel):
    id: int
    brand_name: str
    brand_name_normalized: str | None


class ProductRef(BaseModel):
    """PDP '비슷한 제품' 카드 렌더용 경량 참조 (full ProductDetail 미사용)."""

    id: int
    brand: str
    name: str
    price: float | None
    original_price: float | None
    sale_price: float | None
    image_url: str
    product_url: str


class ProductDetail(BaseModel):
    id: int
    brand: str
    name: str
    category: str | None
    subcategory: str | None
    price: float | None
    original_price: float | None
    sale_price: float | None
    image_url: str
    images: list[str] | None
    product_url: str
    in_stock: bool
    platform: str
    gender: list[str] | None
    # 2026-07-29 — color 출처가 products.color → product_features.feature_metadata
    # ->>'primary_color' (VLM) 로 이관. products.description 은 제거됨 (모바일
    # PDP 가 렌더링하지 않아 소비처가 없었다).
    color: str | None
    tags: list[str] | None
    brand_node: BrandNode | None
    similar: list[ProductRef] = Field(default_factory=list)


class RecordViewRequest(BaseModel):
    session_id: UUID | None = None
    source_search_id: UUID | None = None
    dwell_ms: int | None = None
    source: str | None = Field(default=None, max_length=50)
    section_id: str | None = Field(default=None, max_length=100)


class RecordViewResponse(BaseModel):
    recorded: bool
    view_id: str | None = None


class OutboundRequest(BaseModel):
    source: Literal["curation", "search", "pdp", "wishlist", "history"] | None = None
    section_id: str | None = Field(default=None, max_length=100)


class OutboundResponse(BaseModel):
    recorded: bool


class ViewedProduct(BaseModel):
    product_id: int
    brand: str
    name: str
    price: float | None
    image_url: str
    product_url: str
    viewed_at: str
    source_search_id: str | None


class ViewedListResponse(BaseModel):
    items: list[ViewedProduct]
    next_cursor: str | None


class LinkCheckResponse(BaseModel):
    alive: bool
    last_checked_at: str
    http_status: int | None
    alternative_url: str | None


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_product(pool: AsyncConnectionPool, product_id: int) -> ProductDetail | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                p.id, p.brand, p.name, p.category, p.subcategory,
                p.price, p.original_price, p.sale_price,
                p.image_url, p.images, p.product_url,
                p.in_stock, p.platform,
                -- products.gender 단일 출처. chk_products_gender_required 가
                -- VALIDATE 되어 non-NULL·non-empty·canonical 이 보장되므로
                -- VLM 폴백 CASE 를 걷어냈다.
                p.gender,
                pf.feature_metadata->>'primary_color' AS color, p.tags,
                p.brand_node_id,
                bn.brand_name, bn.brand_name_normalized
            FROM public.products p
            LEFT JOIN public.brand_nodes bn ON bn.id = p.brand_node_id
            LEFT JOIN public.product_features pf ON pf.product_id = p.id
            WHERE p.id = %s
            """,
            (product_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    brand_node = (
        BrandNode(id=row[16], brand_name=row[17], brand_name_normalized=row[18]) if row[16] is not None else None
    )
    return ProductDetail(
        id=row[0],
        brand=row[1],
        name=row[2],
        category=row[3],
        subcategory=row[4],
        price=float(row[5]) if row[5] is not None else None,
        original_price=float(row[6]) if row[6] is not None else None,
        sale_price=float(row[7]) if row[7] is not None else None,
        image_url=row[8],
        images=list(row[9]) if row[9] else None,
        product_url=row[10],
        in_stock=row[11],
        platform=row[12],
        gender=list(row[13]) if row[13] else None,
        color=row[14],
        tags=list(row[15]) if row[15] else None,
        brand_node=brand_node,
    )


async def _get_similar(pool: AsyncConnectionPool, product_id: int, limit: int = 10) -> list[ProductRef]:
    """기준 상품 임베딩과 코사인 거리가 가까운 in-stock 상품 top-N (자기 자신 제외).

    fail-open: 임베딩이 없거나 쿼리 실패 시 빈 리스트 (PDP fetch를 깨뜨리지 않음).
    anchor 임베딩이 NULL이면 WHERE 절이 전체를 제외 → 빈 결과.
    """
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                WITH anchor AS (
                    SELECT embedding FROM public.product_embeddings WHERE product_id = %s
                )
                SELECT p.id, p.brand, p.name, p.price,
                       p.original_price, p.sale_price,
                       p.image_url, p.product_url
                FROM public.product_embeddings e
                JOIN public.products p ON p.id = e.product_id
                WHERE e.product_id <> %s
                  AND p.in_stock
                  AND (SELECT embedding FROM anchor) IS NOT NULL
                ORDER BY e.embedding <=> (SELECT embedding FROM anchor)
                LIMIT %s
                """,
                (product_id, product_id, limit),
            )
            rows = await cur.fetchall()
    except Exception:
        logger.exception("[products] similar fetch failed product=%s", product_id)
        return []

    return [
        ProductRef(
            id=r[0],
            brand=r[1],
            name=r[2],
            price=float(r[3]) if r[3] is not None else None,
            original_price=float(r[4]) if r[4] is not None else None,
            sale_price=float(r[5]) if r[5] is not None else None,
            image_url=r[6],
            product_url=r[7],
        )
        for r in rows
    ]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/products/viewed", response_model=ViewedListResponse, status_code=status.HTTP_200_OK)
async def list_viewed(
    session_id: UUID = Query(..., description="필터링할 세션 ID"),
    cursor: str | None = Query(default=None, description="Pagination cursor (view_id)"),
    limit: int = Query(default=20, ge=1, le=100),
    dedup: bool = Query(default=True, description="동일 상품 중복 제거"),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> ViewedListResponse:
    """세션 기준 조회한 상품 목록."""
    async with pool.connection() as conn, conn.cursor() as cur:
        if dedup:
            # 상품별 가장 최근 조회만 반환 (DISTINCT ON)
            if cursor:
                await cur.execute(
                    """
                    SELECT view_id, product_id, brand, name, price, image_url, product_url,
                           viewed_at, source_search_id
                    FROM (
                        SELECT DISTINCT ON (pv.product_id)
                            pv.view_id, pv.product_id, p.brand, p.name, p.price,
                            p.image_url, p.product_url,
                            pv.viewed_at, pv.source_search_id
                        FROM ai.product_views pv
                        JOIN public.products p ON p.id = pv.product_id
                        WHERE pv.user_id = %s AND pv.session_id = %s
                        ORDER BY pv.product_id, pv.viewed_at DESC
                    ) sub
                    WHERE viewed_at < (
                        SELECT viewed_at FROM ai.product_views WHERE view_id = %s
                    )
                    ORDER BY viewed_at DESC
                    LIMIT %s
                    """,
                    (user_id, session_id, UUID(cursor), limit + 1),
                )
            else:
                await cur.execute(
                    """
                    SELECT view_id, product_id, brand, name, price, image_url, product_url,
                           viewed_at, source_search_id
                    FROM (
                        SELECT DISTINCT ON (pv.product_id)
                            pv.view_id, pv.product_id, p.brand, p.name, p.price,
                            p.image_url, p.product_url,
                            pv.viewed_at, pv.source_search_id
                        FROM ai.product_views pv
                        JOIN public.products p ON p.id = pv.product_id
                        WHERE pv.user_id = %s AND pv.session_id = %s
                        ORDER BY pv.product_id, pv.viewed_at DESC
                    ) sub
                    ORDER BY viewed_at DESC
                    LIMIT %s
                    """,
                    (user_id, session_id, limit + 1),
                )
        else:
            if cursor:
                await cur.execute(
                    """
                    SELECT pv.view_id, pv.product_id, p.brand, p.name, p.price,
                           p.image_url, p.product_url, pv.viewed_at, pv.source_search_id
                    FROM ai.product_views pv
                    JOIN public.products p ON p.id = pv.product_id
                    WHERE pv.user_id = %s AND pv.session_id = %s
                      AND pv.viewed_at < (
                          SELECT viewed_at FROM ai.product_views WHERE view_id = %s
                      )
                    ORDER BY pv.viewed_at DESC
                    LIMIT %s
                    """,
                    (user_id, session_id, UUID(cursor), limit + 1),
                )
            else:
                await cur.execute(
                    """
                    SELECT pv.view_id, pv.product_id, p.brand, p.name, p.price,
                           p.image_url, p.product_url, pv.viewed_at, pv.source_search_id
                    FROM ai.product_views pv
                    JOIN public.products p ON p.id = pv.product_id
                    WHERE pv.user_id = %s AND pv.session_id = %s
                    ORDER BY pv.viewed_at DESC
                    LIMIT %s
                    """,
                    (user_id, session_id, limit + 1),
                )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1][0]) if has_more and page else None

    items = [
        ViewedProduct(
            product_id=r[1],
            brand=r[2],
            name=r[3],
            price=float(r[4]) if r[4] is not None else None,
            image_url=r[5],
            product_url=r[6],
            viewed_at=r[7].isoformat(),
            source_search_id=str(r[8]) if r[8] else None,
        )
        for r in page
    ]
    return ViewedListResponse(items=items, next_cursor=next_cursor)


@router.get("/products/{product_id}", response_model=ProductDetail, status_code=status.HTTP_200_OK)
async def get_product(
    product_id: int,
    search_id: UUID | None = Query(default=None),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> ProductDetail:
    """상품 상세 조회 (crawler DB). search_id: 향후 추적용 예약 파라미터(현재 미사용)."""
    product = await _get_product(pool, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    product.similar = await _get_similar(pool, product_id)
    return product


@router.post("/products/{product_id}/view", response_model=RecordViewResponse, status_code=status.HTTP_200_OK)
async def record_view(
    product_id: int,
    body: RecordViewRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> RecordViewResponse:
    """상품 조회 기록. 24시간 내 동일 상품 중복 조회는 무시."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.product_views
                (user_id, product_id, session_id, source_search_id, dwell_ms)
            SELECT %s, %s, %s, %s, %s
            WHERE NOT EXISTS (
                SELECT 1 FROM ai.product_views
                WHERE user_id = %s
                  AND product_id = %s
                  AND viewed_at > now() - INTERVAL '24 hours'
            )
            RETURNING view_id
            """,
            (
                user_id,
                product_id,
                body.session_id or uuid4(),
                body.source_search_id,
                body.dwell_ms,
                user_id,
                product_id,
            ),
        )
        row = await cur.fetchone()

    if row:
        await record_product_signal(
            pool,
            user_id=user_id,
            product_id=product_id,
            signal_type="view",
            dedupe_key=f"view:{user_id}:{product_id}:{datetime.now(tz=UTC).date()}",
            metadata={"source": body.source, "section_id": body.section_id},
        )
        return RecordViewResponse(recorded=True, view_id=str(row[0]))
    return RecordViewResponse(recorded=False)


@router.post("/products/{product_id}/outbound", response_model=OutboundResponse)
async def record_outbound(
    product_id: int,
    body: OutboundRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> OutboundResponse:
    recorded = await record_product_signal(
        pool,
        user_id=user_id,
        product_id=product_id,
        signal_type="outbound",
        dedupe_key=f"outbound:{user_id}:{product_id}:{datetime.now(tz=UTC).date()}",
        metadata={"source": body.source, "section_id": body.section_id},
    )
    return OutboundResponse(recorded=recorded)


@router.post("/products/{product_id}/link-check", response_model=LinkCheckResponse, status_code=status.HTTP_200_OK)
async def link_check(
    product_id: int,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> LinkCheckResponse:
    """상품 URL 유효성 확인."""
    product = await _get_product(pool, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    from datetime import UTC, datetime

    checked_at = datetime.now(UTC).isoformat()
    try:
        # HEAD 를 지원하지 않는 쇼핑몰이 많아 405/501 을 반환 → 정상 상품이 dead 로 오판됨.
        # GET 으로 상태를 확인하되, 본문은 스트림으로 열어 상태 코드만 읽고 끊어 전량 다운로드를 피한다.
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0, headers=_LINK_CHECK_HEADERS) as client:
            async with client.stream("GET", product.product_url) as resp:
                status_code = resp.status_code
                final_url = str(resp.url)
        # 봇 차단(401/403/429)은 dead 가 아니라 anti-bot 이므로 fail-open 으로 살아있게 둔다.
        alive = status_code < 400 or status_code in _ANTIBOT_STATUSES
        return LinkCheckResponse(
            alive=alive,
            last_checked_at=checked_at,
            http_status=status_code,
            alternative_url=final_url if final_url != product.product_url else None,
        )
    except (httpx.RequestError, TimeoutError):
        return LinkCheckResponse(
            alive=False,
            last_checked_at=checked_at,
            http_status=None,
            alternative_url=None,
        )
