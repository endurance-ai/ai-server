"""Brand search API (온보딩 ④ 고정 검색창).

GET /v1/brands/search?q= — 대표 브랜드 그리드에 없는 브랜드 추가용 (no auth)
"""

from __future__ import annotations

import base64
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id, get_optional_user_id
from app.core.di import provide_db_pool
from app.infrastructure.repositories.brand_node_cache import normalize_brand
from app.services.notifications import brand_news_copy

router = APIRouter(prefix="/v1", tags=["brands"])

_SEARCH_LIMIT = 8
# 브랜드 홈 "최근 소식" 노출 건수. 타임라인이 아니라 헤즈업이라 짧게 자른다.
_NEWS_LIMIT = 5


class BrandSearchItem(BaseModel):
    id: int
    name: str
    node_id: int | None


class BrandSearchResponse(BaseModel):
    brands: list[BrandSearchItem]


@router.get("/brands/search", response_model=BrandSearchResponse)
async def search_brands(
    q: str = Query(min_length=1, max_length=64),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> BrandSearchResponse:
    """브랜드명 부분 일치 검색. 정규화 prefix 매치를 우선 정렬 (예: 'alyx' → '1017 ALYX 9SM')."""
    normalized = normalize_brand(q)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, brand_name, primary_style_node_id
            FROM public.brand_nodes
            WHERE brand_name ILIKE %(like)s
               OR (%(norm)s <> '' AND brand_name_normalized LIKE %(norm_like)s)
            ORDER BY (brand_name_normalized LIKE %(norm_like)s) DESC, length(brand_name) ASC, brand_name ASC
            LIMIT %(limit)s
            """,
            {
                "like": f"%{q}%",
                "norm": normalized,
                "norm_like": f"{normalized}%" if normalized else "",
                "limit": _SEARCH_LIMIT,
            },
        )
        rows = await cur.fetchall()
    return BrandSearchResponse(
        brands=[BrandSearchItem(id=r[0], name=r[1], node_id=int(r[2]) if r[2] is not None else None) for r in rows]
    )


# ── Follow ─────────────────────────────────────────────────────────────────────
#
# ai.user_brand_picks 를 팔로우 저장소로 그대로 재사용한다 (온보딩 픽과 같은 테이블,
# source 로 구분). notify_enabled 로 브랜드 알림 on/off 를 표현한다. 알림 on/off 변경도
# 이 POST 재호출로 처리 — 별도 PATCH 라우트는 없다.


class FollowRequest(BaseModel):
    notify: bool = True


class FollowResponse(BaseModel):
    following: bool
    notify_enabled: bool


class UnfollowResponse(BaseModel):
    following: bool


class FollowItem(BaseModel):
    brand_id: int
    brand_name: str
    notify_enabled: bool


class FollowListResponse(BaseModel):
    items: list[FollowItem]
    next_cursor: str | None


class BrandNewsItem(BaseModel):
    """브랜드 소식 한 건. ai.brand_news 정본 1행 = 이 카드 1개 (migration 0027)."""

    id: int
    kind: str
    text: str
    sub: str
    started_at: str
    ended_at: str | None
    """세일 종료 시각. null 이면 진행 중 — 프론트가 '진행 중' 배지를 붙일 근거."""


class BrandHome(BaseModel):
    id: int
    name: str
    description: str | None
    logo_url: str | None
    product_count: int
    following: bool
    notify_enabled: bool
    store_url: str | None
    """공식 스토어 방문 링크. brand_nodes.wiki->>'homepage_url' 소스."""
    news: list[BrandNewsItem]
    """최근 소식 (최신순). 알림 배치가 ai.brand_news 에 남긴 정본을 그대로 읽는다.

    0027 이전엔 brand_nodes.wiki->>'news' 단문을 읽었는데, 그 키를 쓰는 writer 가
    코드베이스에 없어 항상 null 이었다 (수동 관리 전제였으나 관리 주체 부재).
    """


class BrandNewsResponse(BaseModel):
    items: list[BrandNewsItem]
    next_cursor: str | None


class BrandProduct(BaseModel):
    id: int
    brand: str
    name: str
    price: float | None
    original_price: float | None
    sale_price: float | None
    image_url: str
    product_url: str


class BrandProductsResponse(BaseModel):
    items: list[BrandProduct]
    next_cursor: str | None


@router.post("/brands/{brand_id}/follow", response_model=FollowResponse, status_code=status.HTTP_200_OK)
async def follow_brand(
    brand_id: int,
    body: FollowRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> FollowResponse:
    """브랜드 팔로우(또는 알림 on/off 변경). 존재하지 않는 브랜드는 404."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM public.brand_nodes WHERE id = %s", (brand_id,))
        if not await cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

        await cur.execute(
            """
            INSERT INTO ai.user_brand_picks (user_id, brand_id, notify_enabled, source)
            VALUES (%s, %s, %s, 'follow')
            ON CONFLICT (user_id, brand_id) DO UPDATE SET notify_enabled = EXCLUDED.notify_enabled
            RETURNING notify_enabled
            """,
            (user_id, brand_id, body.notify),
        )
        notify_enabled = (await cur.fetchone())[0]
        await conn.commit()

    return FollowResponse(following=True, notify_enabled=notify_enabled)


@router.delete("/brands/{brand_id}/follow", response_model=UnfollowResponse, status_code=status.HTTP_200_OK)
async def unfollow_brand(
    brand_id: int,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> UnfollowResponse:
    """언팔로우. 팔로우 중이 아니어도 200 (멱등)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM ai.user_brand_picks WHERE user_id = %s AND brand_id = %s",
            (user_id, brand_id),
        )
        await conn.commit()
    return UnfollowResponse(following=False)


def _news_item(row: tuple) -> BrandNewsItem:
    """(id, kind, payload, started_at, ended_at) → 카드. 브랜드 홈과 더보기가 공유한다."""
    text, sub = brand_news_copy(row[1], row[2] or {})
    return BrandNewsItem(
        id=row[0],
        kind=row[1],
        text=text,
        sub=sub,
        started_at=row[3].isoformat(),
        ended_at=row[4].isoformat() if row[4] else None,
    )


# 브랜드 홈 프리뷰와 더보기가 같은 정렬을 써야 페이지 경계에서 항목이 새거나 겹치지
# 않는다. started_at 만으로는 부족하다 — 같은 배치가 여러 브랜드 소식을 같은 시각으로
# 남기므로 id 를 tie-breaker 로 둔다.
_BRAND_NEWS_SELECT = """
    SELECT id, kind, payload, started_at, ended_at
    FROM ai.brand_news
    WHERE brand_node_id = %(brand_id)s
      AND (
          %(cur_at)s::timestamptz IS NULL
          OR (started_at, id) < (%(cur_at)s::timestamptz, %(cur_id)s::bigint)
      )
    ORDER BY started_at DESC, id DESC
    LIMIT %(lim)s
"""


def _decode_news_cursor(cursor: str | None) -> tuple[str | None, int | None]:
    if not cursor:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts_part, id_part = raw.rsplit("|", 1)
        return ts_part, int(id_part)
    except (ValueError, UnicodeDecodeError):
        return None, None


def _encode_news_cursor(started_at_iso: str, news_id: int) -> str:
    return base64.urlsafe_b64encode(f"{started_at_iso}|{news_id}".encode()).decode("ascii")


def _decode_follows_cursor(cursor: str | None) -> tuple[str | None, int | None]:
    """base64url 토큰 → (created_at ISO, brand_id). 못 읽으면 커서 없음으로 폴백."""
    if not cursor:
        return None, None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts_part, bid_part = raw.rsplit("|", 1)
        return ts_part, int(bid_part)
    except (ValueError, UnicodeDecodeError):
        return None, None


def _encode_follows_cursor(created_at_iso: str, brand_id: int) -> str:
    """opaque 토큰으로 인코딩한다 — ISO 타임스탬프의 `+00:00` 이 쿼리스트링에서 공백으로
    치환되는 문제(+ 는 application/x-www-form-urlencoded 에서 공백)를 원천 차단한다.
    클라이언트가 percent-encoding 없이 URL에 그대로 이어붙여도 안전하다."""
    raw = f"{created_at_iso}|{brand_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


@router.get("/me/follows", response_model=FollowListResponse, status_code=status.HTTP_200_OK)
async def list_follows(
    cursor: str | None = Query(default=None, description="Pagination cursor (opaque token)"),
    limit: int = Query(default=20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> FollowListResponse:
    """팔로우한 브랜드 목록 (최신순). 온보딩 픽·직접 팔로우 모두 포함."""
    cursor_ts, cursor_bid = _decode_follows_cursor(cursor)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT ubp.brand_id, bn.brand_name, ubp.notify_enabled, ubp.created_at
            FROM ai.user_brand_picks ubp
            JOIN public.brand_nodes bn ON bn.id = ubp.brand_id
            WHERE ubp.user_id = %s
              AND (
                  %s::timestamptz IS NULL
                  OR (ubp.created_at, ubp.brand_id) < (%s::timestamptz, %s::bigint)
              )
            ORDER BY ubp.created_at DESC, ubp.brand_id DESC
            LIMIT %s
            """,
            (user_id, cursor_ts, cursor_ts, cursor_bid, limit + 1),
        )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _encode_follows_cursor(page[-1][3].isoformat(), page[-1][0]) if has_more and page else None

    items = [FollowItem(brand_id=r[0], brand_name=r[1], notify_enabled=r[2]) for r in page]
    return FollowListResponse(items=items, next_cursor=next_cursor)


@router.get("/brands/{brand_id}", response_model=BrandHome, status_code=status.HTTP_200_OK)
async def brand_home(
    brand_id: int,
    user_id: UUID | None = Depends(get_optional_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> BrandHome:
    """브랜드 홈. 비로그인도 조회 가능 (로그인 시 following/notify_enabled 채움)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, brand_name, description, wiki FROM public.brand_nodes WHERE id = %s",
            (brand_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

        await cur.execute("SELECT count(*) FROM public.products WHERE brand_node_id = %s", (brand_id,))
        product_count = (await cur.fetchone())[0]

        following = False
        notify_enabled = False
        if user_id is not None:
            await cur.execute(
                "SELECT notify_enabled FROM ai.user_brand_picks WHERE user_id = %s AND brand_id = %s",
                (user_id, brand_id),
            )
            follow_row = await cur.fetchone()
            if follow_row:
                following = True
                notify_enabled = follow_row[0]

        # 소식은 팔로우 여부·로그인 여부와 무관하게 브랜드 단위로 읽는다 — 정본이
        # ai.brand_news 에 있어서 가능하다. 유저별 팬아웃 행을 뒤질 때는 팔로워가
        # 없는 브랜드의 홈이 영영 비어 있었다 (migration 0027 docstring 참조).
        await cur.execute(
            _BRAND_NEWS_SELECT,
            {"brand_id": brand_id, "cur_at": None, "cur_id": None, "lim": _NEWS_LIMIT},
        )
        news_rows = await cur.fetchall()

    # description 은 brand_nodes.description 전용 컬럼이 단일 출처다 (kiko.ai-app
    # migration 105 — wiki.description_ko/description_original 은 이 컬럼으로
    # 백필된 뒤 wiki 에서 삭제됐다. 예전에 여기서 읽던 wiki.get("description")는
    # 애초에 존재한 적 없는 키라 항상 null 을 반환하고 있었다.
    # logo_url 은 brand_nodes 에 전용 컬럼이 없다 — 없는 컬럼을 지어내지 않는다.
    wiki = row[3] or {}
    store_url = wiki.get("homepage_url") if isinstance(wiki, dict) else None

    return BrandHome(
        id=row[0],
        name=row[1],
        description=row[2],
        logo_url=None,
        product_count=product_count,
        following=following,
        notify_enabled=notify_enabled,
        store_url=store_url,
        news=[_news_item(r) for r in news_rows],
    )


@router.get("/brands/{brand_id}/news", response_model=BrandNewsResponse, status_code=status.HTTP_200_OK)
async def brand_news(
    brand_id: int,
    cursor: str | None = Query(default=None, description="Pagination cursor (opaque token)"),
    limit: int = Query(default=20, ge=1, le=100),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> BrandNewsResponse:
    """브랜드 소식 전체 목록 (최신순) — 브랜드 홈 '더보기'.

    홈은 프리뷰 _NEWS_LIMIT 건만 보여주므로 그 뒤를 이어 받는 경로가 필요하다.
    브랜드 홈과 같은 정렬·같은 문구 변환을 쓴다 (_BRAND_NEWS_SELECT / _news_item).

    브랜드 홈과 마찬가지로 인증이 필요 없다 — 소식은 팔로우·로그인과 무관한 브랜드
    단위 사실이다. 존재하지 않는 브랜드는 404 로 구분해 준다: 소식이 없는 것과
    브랜드가 없는 것은 클라이언트에 다른 의미다.
    """
    cursor_at, cursor_id = _decode_news_cursor(cursor)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM public.brand_nodes WHERE id = %s", (brand_id,))
        if not await cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Brand not found")

        await cur.execute(
            _BRAND_NEWS_SELECT,
            {"brand_id": brand_id, "cur_at": cursor_at, "cur_id": cursor_id, "lim": limit + 1},
        )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _encode_news_cursor(page[-1][3].isoformat(), page[-1][0]) if has_more and page else None
    return BrandNewsResponse(items=[_news_item(r) for r in page], next_cursor=next_cursor)


@router.get("/brands/{brand_id}/products", response_model=BrandProductsResponse, status_code=status.HTTP_200_OK)
async def brand_products(
    brand_id: int,
    cursor: str | None = Query(default=None, description="Pagination cursor (product id)"),
    gender: str | None = Query(default=None, description="women / men 등 성별 필터"),
    limit: int = Query(default=20, ge=1, le=100),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> BrandProductsResponse:
    """브랜드 상품 목록 (id DESC keyset). gender 지정 시 products.gender 배열 매칭."""
    cursor_id = int(cursor) if cursor and cursor.isdigit() else None

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, brand, name, price, original_price, sale_price, image_url, product_url
            FROM public.products
            WHERE brand_node_id = %s
              AND (%s::bigint IS NULL OR id < %s::bigint)
              AND (%s::text IS NULL OR %s = ANY(gender))
            ORDER BY id DESC
            LIMIT %s
            """,
            (brand_id, cursor_id, cursor_id, gender, gender, limit + 1),
        )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1][0]) if has_more and page else None

    items = [
        BrandProduct(
            id=r[0],
            brand=r[1],
            name=r[2],
            price=float(r[3]) if r[3] is not None else None,
            original_price=float(r[4]) if r[4] is not None else None,
            sale_price=float(r[5]) if r[5] is not None else None,
            image_url=r[6],
            product_url=r[7],
        )
        for r in page
    ]
    return BrandProductsResponse(items=items, next_cursor=next_cursor)
