"""Saves (찜) API.

POST   /v1/saves               — 찜 추가
GET    /v1/saves               — 찜 목록 (cursor 페이지네이션)
DELETE /v1/saves/{product_id}  — 찜 해제

Note: product 상세(brand/name/price)는 crawler DB 연동 시 추가 예정.
현재는 save_id / product_id / created_at 만 반환.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["saves"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class AddSaveRequest(BaseModel):
    product_id: str


class SaveItem(BaseModel):
    save_id: str
    product_id: str
    created_at: str


class SaveListResponse(BaseModel):
    items: list[SaveItem]
    next_cursor: str | None
    total: int


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/saves", response_model=SaveItem, status_code=status.HTTP_201_CREATED)
async def add_save(
    body: AddSaveRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> SaveItem:
    """찜 추가. 이미 저장된 경우 409."""
    if not body.product_id.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="product_id cannot be empty")

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.saves (user_id, product_id)
            VALUES (%s, %s)
            ON CONFLICT (user_id, product_id) DO NOTHING
            RETURNING save_id, product_id, created_at
            """,
            (user_id, body.product_id),
        )
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already saved")
    return SaveItem(save_id=str(row[0]), product_id=row[1], created_at=row[2].isoformat())


@router.get("/saves", response_model=SaveListResponse, status_code=status.HTTP_200_OK)
async def list_saves(
    cursor: str | None = Query(default=None, description="Pagination cursor (save_id)"),
    limit: int = Query(default=20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> SaveListResponse:
    """찜 목록 조회 (최신순)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM ai.saves WHERE user_id = %s", (user_id,))
        total = (await cur.fetchone())[0]

        if cursor:
            await cur.execute(
                """
                SELECT save_id, product_id, created_at
                FROM ai.saves
                WHERE user_id = %s
                  AND created_at < (SELECT created_at FROM ai.saves WHERE save_id = %s AND user_id = %s)
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, UUID(cursor), user_id, limit + 1),
            )
        else:
            await cur.execute(
                """
                SELECT save_id, product_id, created_at
                FROM ai.saves
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit + 1),
            )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1][0]) if has_more and page else None

    return SaveListResponse(
        items=[SaveItem(save_id=str(r[0]), product_id=r[1], created_at=r[2].isoformat()) for r in page],
        next_cursor=next_cursor,
        total=total,
    )


@router.delete("/saves/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_save(
    product_id: str,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> None:
    """찜 해제."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM ai.saves WHERE user_id = %s AND product_id = %s RETURNING save_id",
            (user_id, product_id),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Save not found")
