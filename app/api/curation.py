"""Main curation API.

GET /v1/curation?gender=women|men — 메인 큐레이션 구좌 + 유도 칩 (auth optional)

server-driven: 구좌 개수·순서·타이틀·상품 전부 이 응답으로 결정 — 앱 배포 불필요.
구좌 데이터는 ai.curation_sections 캐시 테이블(백그라운드 refresher가 채움)만 읽고,
요청 경로에서는 집계하지 않는다. 칩은 서버 상수(app/services/curation_chips.py).

gender 해석: 로그인 + 프로필 gender 확정이면 프로필 우선, 아니면 query param
(비로그인은 온보딩 로컬값을 param으로 전달), 둘 다 없으면 422.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_id, get_optional_user_id
from app.core.di import provide_db_pool
from app.core.gender import db_to_app
from app.services.curation_chips import Chip, chips_for
from app.services.curation_refresh import (
    GENDER_MATCH_SQL,
    PRODUCT_FEATURES_JOIN,
    select_candidate_ids,
)
from app.services.curation_taste import record_impressions

router = APIRouter(prefix="/v1", tags=["curation"])

_PRODUCTS_PER_SECTION = 20


# ── Schemas ───────────────────────────────────────────────────────────────────


class CurationProduct(BaseModel):
    """기존 검색 응답 Product 스키마 재사용 (results.py ResultProduct와 동일 필드)."""

    product_id: int
    brand: str
    name: str
    price: float | None
    image_url: str
    product_url: str


class CurationSection(BaseModel):
    id: str
    slot_type: Literal["auto", "editorial"]
    title: str
    subtitle: str | None
    products: list[CurationProduct]


class CurationResponse(BaseModel):
    gender: Literal["women", "men"]
    sections: list[CurationSection]
    chips: list[Chip]


class CurationImpression(BaseModel):
    section_id: str = Field(min_length=1, max_length=100)
    product_id: int
    position: int | None = Field(default=None, ge=0, le=100)


class CurationImpressionRequest(BaseModel):
    items: list[CurationImpression] = Field(min_length=1, max_length=50)


class CurationImpressionResponse(BaseModel):
    recorded: int


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _profile_gender(pool: AsyncConnectionPool, user_id: UUID) -> str | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT gender FROM ai.user_profiles WHERE user_id = %s", (user_id,))
        row = await cur.fetchone()
    return db_to_app(row[0]) if row else None


async def _load_sections(
    pool: AsyncConnectionPool,
    gender: str,
    user_id: UUID | None = None,
) -> list[CurationSection]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT section_id, slot_type, title, subtitle, product_ids
            FROM ai.curation_sections
            WHERE gender = %s AND is_active
            ORDER BY sort_order ASC, section_id ASC
            """,
            (gender,),
        )
        section_rows = await cur.fetchall()

        selected_by_section: dict[str, list[int]] = {}
        excluded_ids: set[int] = set()
        taste_scores: dict[int, float] = {}
        if user_id is not None:
            await cur.execute(
                """
                SELECT style_node_id,
                       greatest(-20.0, least(
                           20.0,
                           score * power(
                               0.5,
                               greatest(0, extract(epoch FROM (now() - last_event_at)))
                                   / (30 * 24 * 60 * 60)
                           )
                       ))
                FROM ai.user_style_scores
                WHERE user_id = %s
                """,
                (user_id,),
            )
            taste_scores = {int(r[0]): float(r[1]) for r in await cur.fetchall()}

        for section_id, slot_type, _title, _subtitle, product_ids in section_rows:
            if slot_type != "auto" or user_id is None or not taste_scores:
                selected = [
                    int(pid) for pid in (product_ids or [])[:_PRODUCTS_PER_SECTION] if int(pid) not in excluded_ids
                ]
            else:
                await cur.execute(
                    """
                    SELECT product_id, is_hot, base_score, base_rank, brand_key,
                           brand_node_id, style_node_id
                    FROM ai.curation_candidates
                    WHERE section_id = %s AND gender = %s
                    ORDER BY is_hot DESC, base_rank
                    """,
                    (section_id, gender),
                )
                candidate_rows = [
                    {
                        "product_id": int(r[0]),
                        "is_hot": bool(r[1]),
                        "base_score": float(r[2]),
                        "base_rank": int(r[3]),
                        "brand_key": r[4],
                        "brand_node_id": r[5],
                        "style_node_id": int(r[6]) if r[6] is not None else None,
                    }
                    for r in await cur.fetchall()
                ]
                selected = select_candidate_ids(
                    candidate_rows,
                    section_id=section_id,
                    excluded_ids=excluded_ids,
                    taste_scores=taste_scores,
                    seed=f"{user_id}:{gender}:{section_id}",
                )
                if not selected:
                    selected = [
                        int(pid) for pid in (product_ids or [])[:_PRODUCTS_PER_SECTION] if int(pid) not in excluded_ids
                    ]
            selected_by_section[section_id] = selected
            excluded_ids.update(selected)

        # 구좌별 product_ids를 한 번에 하이드레이션 (results.py의 unnest 패턴).
        all_ids: list[int] = []
        for r in section_rows:
            all_ids.extend(selected_by_section.get(r[0], []))
        products: dict[int, CurationProduct] = {}
        if all_ids:
            # 성별 술어는 curation_refresh 와 같은 3단 다리를 공유한다 — 후보를
            # 뽑을 때와 하이드레이션할 때의 기준이 갈리면 구좌가 조용히 빈다.
            await cur.execute(
                f"""
                SELECT p.id, p.brand, p.name, p.price, p.image_url, p.product_url
                FROM public.products p
                {PRODUCT_FEATURES_JOIN}
                WHERE p.id = ANY(%(ids)s)
                  AND p.in_stock
                  AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
                  AND p.price >= 5000
                  AND {GENDER_MATCH_SQL}
                """,  # noqa: S608 -- 보간되는 값은 모두 모듈 소유 상수
                {"ids": list(dict.fromkeys(all_ids)), "gender": gender},
            )
            for pid, brand, name, price, image_url, product_url in await cur.fetchall():
                products[int(pid)] = CurationProduct(
                    product_id=int(pid),
                    brand=brand,
                    name=name,
                    price=float(price) if price is not None else None,
                    image_url=image_url,
                    product_url=product_url,
                )

    sections: list[CurationSection] = []
    for section_id, slot_type, title, subtitle, product_ids in section_rows:
        selected = selected_by_section.get(section_id, (product_ids or [])[:_PRODUCTS_PER_SECTION])
        hydrated = [products[pid] for pid in selected if pid in products]
        sections.append(
            CurationSection(id=section_id, slot_type=slot_type, title=title, subtitle=subtitle, products=hydrated)
        )
    return sections


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("/curation", response_model=CurationResponse)
async def get_curation(
    response: Response,
    gender: Literal["women", "men"] | None = Query(default=None),
    user_id: UUID | None = Depends(get_optional_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> CurationResponse:
    """메인 큐레이션. 구좌 0개여도 200 + 빈 배열 (클라는 마지막 성공 응답 캐시로 폴백)."""
    resolved = None
    if user_id is not None:
        resolved = await _profile_gender(pool, user_id)  # 로그인은 프로필 우선
    resolved = resolved or gender
    if resolved not in ("women", "men"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="gender is required (query param for guests, profile for logged-in users)",
        )

    sections = await _load_sections(pool, resolved, user_id)
    response.headers["Cache-Control"] = "private, no-store"
    return CurationResponse(gender=resolved, sections=sections, chips=chips_for(resolved))


@router.post("/curation/impressions", response_model=CurationImpressionResponse)
async def create_curation_impressions(
    body: CurationImpressionRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> CurationImpressionResponse:
    recorded = await record_impressions(
        pool,
        user_id=user_id,
        items=[(item.section_id, item.product_id, item.position) for item in body.items],
    )
    return CurationImpressionResponse(recorded=recorded)
