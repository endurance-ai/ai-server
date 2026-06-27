"""Result-set history API.

GET /v1/results              — list listed result sets in a session
GET /v1/results/{search_id}  — paginate a single result set (opens it = is_listed)

A "result set" is the ranked product list produced by one search turn, persisted
to ai.searches (product_ids BIGINT[] in cosine order). /v1/results surfaces only
sets the user opened as a list view (is_listed=true); opening a set via
GET /v1/results/{search_id} flips that flag.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["results"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class ResultSetSummary(BaseModel):
    search_id: str
    query_text: str
    result_count: int
    preview_images: list[str]
    created_at: str


class ResultSetListResponse(BaseModel):
    items: list[ResultSetSummary]
    next_cursor: str | None


class ResultProduct(BaseModel):
    rank: int
    product_id: int
    brand: str
    name: str
    price: float | None
    image_url: str
    product_url: str


class ResultSetPageResponse(BaseModel):
    search_id: str
    query_text: str
    result_count: int
    items: list[ResultProduct]
    next_cursor: str | None


# ── Endpoints ─────────────────────────────────────────────────────────────────

_PREVIEW_SUBQUERY = """
    (SELECT array_agg(p.image_url ORDER BY t.ord)
     FROM unnest(s.product_ids[1:4]) WITH ORDINALITY AS t(pid, ord)
     JOIN public.products p ON p.id = t.pid)
"""


@router.get("/results", response_model=ResultSetListResponse, status_code=status.HTTP_200_OK)
async def list_result_sets(
    session_id: UUID = Query(..., description="세션 ID (단일 세션 한정)"),
    cursor: str | None = Query(default=None, description="Pagination cursor (search_id)"),
    limit: int = Query(default=20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> ResultSetListResponse:
    """세션 내 [더보기]로 펼쳐본 리스트(결과셋) 히스토리. is_listed=true만, 최신순."""
    async with pool.connection() as conn, conn.cursor() as cur:
        if cursor:
            await cur.execute(
                f"""
                SELECT s.search_id, s.query_text, s.result_count, s.created_at, {_PREVIEW_SUBQUERY} AS preview
                FROM ai.searches s
                WHERE s.session_id = %s AND s.user_id = %s AND s.is_listed = TRUE
                  AND s.created_at < (SELECT created_at FROM ai.searches WHERE search_id = %s)
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (session_id, user_id, UUID(cursor), limit + 1),
            )
        else:
            await cur.execute(
                f"""
                SELECT s.search_id, s.query_text, s.result_count, s.created_at, {_PREVIEW_SUBQUERY} AS preview
                FROM ai.searches s
                WHERE s.session_id = %s AND s.user_id = %s AND s.is_listed = TRUE
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (session_id, user_id, limit + 1),
            )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1][0]) if has_more and page else None
    items = [
        ResultSetSummary(
            search_id=str(r[0]),
            query_text=r[1],
            result_count=r[2],
            preview_images=list(r[4]) if r[4] else [],
            created_at=r[3].isoformat(),
        )
        for r in page
    ]
    return ResultSetListResponse(items=items, next_cursor=next_cursor)


@router.get("/results/{search_id}", response_model=ResultSetPageResponse, status_code=status.HTTP_200_OK)
async def get_result_set_page(
    search_id: UUID,
    cursor: str | None = Query(default=None, description="Pagination cursor (rank of last item)"),
    limit: int = Query(default=20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> ResultSetPageResponse:
    """단일 result set의 페이지네이션. 첫 호출 시 is_listed=true로 플립(리스트 화면 열람)."""
    after_rank = 0
    if cursor:
        try:
            after_rank = int(cursor)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid cursor") from None

    async with pool.connection() as conn, conn.cursor() as cur:
        # Opening the set as a list view marks it is_listed (also ownership gate).
        await cur.execute(
            """
            UPDATE ai.searches SET is_listed = TRUE
            WHERE search_id = %s AND user_id = %s
            RETURNING query_text, result_count
            """,
            (search_id, user_id),
        )
        meta = await cur.fetchone()
        if not meta:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Result set not found")

        await cur.execute(
            """
            SELECT t.ord AS rank, t.pid AS product_id,
                   p.brand, p.name, p.price, p.image_url, p.product_url
            FROM ai.searches s
            JOIN LATERAL unnest(s.product_ids) WITH ORDINALITY AS t(pid, ord) ON TRUE
            JOIN public.products p ON p.id = t.pid
            WHERE s.search_id = %s AND s.user_id = %s AND t.ord > %s
            ORDER BY t.ord ASC
            LIMIT %s
            """,
            (search_id, user_id, after_rank, limit + 1),
        )
        rows = await cur.fetchall()
        await conn.commit()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1][0]) if has_more and page else None
    items = [
        ResultProduct(
            rank=r[0],
            product_id=r[1],
            brand=r[2],
            name=r[3],
            price=float(r[4]) if r[4] is not None else None,
            image_url=r[5],
            product_url=r[6],
        )
        for r in page
    ]
    return ResultSetPageResponse(
        search_id=str(search_id),
        query_text=meta[0],
        result_count=meta[1],
        items=items,
        next_cursor=next_cursor,
    )
