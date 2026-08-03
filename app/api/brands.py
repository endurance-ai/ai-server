"""Brand search API (온보딩 ④ 고정 검색창).

GET /v1/brands/search?q= — 대표 브랜드 그리드에 없는 브랜드 추가용 (no auth)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.core.di import provide_db_pool
from app.infrastructure.repositories.brand_node_cache import normalize_brand

router = APIRouter(prefix="/v1", tags=["brands"])

_SEARCH_LIMIT = 8


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
