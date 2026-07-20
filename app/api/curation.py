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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_optional_user_id
from app.core.di import provide_db_pool
from app.core.gender import db_to_app
from app.services.curation_chips import Chip, chips_for

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


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _profile_gender(pool: AsyncConnectionPool, user_id: UUID) -> str | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT gender FROM ai.user_profiles WHERE user_id = %s", (user_id,))
        row = await cur.fetchone()
    return db_to_app(row[0]) if row else None


async def _load_sections(pool: AsyncConnectionPool, gender: str) -> list[CurationSection]:
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

        # 구좌별 product_ids를 한 번에 하이드레이션 (results.py의 unnest 패턴).
        all_ids: list[int] = []
        for r in section_rows:
            all_ids.extend(r[4][:_PRODUCTS_PER_SECTION] if r[4] else [])
        products: dict[int, CurationProduct] = {}
        if all_ids:
            await cur.execute(
                """
                SELECT p.id, p.brand, p.name, p.price, p.image_url, p.product_url
                FROM public.products p
                WHERE p.id = ANY(%s) AND p.in_stock
                """,
                (list(dict.fromkeys(all_ids)),),
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
        hydrated = [products[pid] for pid in (product_ids or [])[:_PRODUCTS_PER_SECTION] if pid in products]
        sections.append(
            CurationSection(id=section_id, slot_type=slot_type, title=title, subtitle=subtitle, products=hydrated)
        )
    return sections


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("/curation", response_model=CurationResponse)
async def get_curation(
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

    sections = await _load_sections(pool, resolved)
    return CurationResponse(gender=resolved, sections=sections, chips=chips_for(resolved))
