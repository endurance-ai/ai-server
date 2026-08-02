"""Saves (찜) API.

POST   /v1/saves               — 찜 추가
GET    /v1/saves               — 찜 목록 (cursor 페이지네이션)
DELETE /v1/saves/{product_id}  — 찜 해제

List Saves는 public.products와 LEFT JOIN 하여 product 상세(brand/name/price/
image_url/in_stock)를 포함한다. 카탈로그에서 사라진 상품은 product=null.
Add Save는 명세대로 {save_id, product_id, created_at} 만 반환.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool
from app.services.curation_taste import record_product_signal

router = APIRouter(prefix="/v1", tags=["saves"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class AddSaveRequest(BaseModel):
    product_id: str


class SaveItem(BaseModel):
    save_id: str
    product_id: str
    created_at: str


class SavedProduct(BaseModel):
    id: int
    brand: str | None
    name: str | None
    price: float | None
    image_url: str | None
    in_stock: bool | None


class SaveListItem(BaseModel):
    save_id: str
    product: SavedProduct | None  # None if the product is no longer in the catalog
    created_at: str


class SaveListResponse(BaseModel):
    items: list[SaveListItem]
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

        if row and body.product_id.isdigit():
            await cur.execute(
                """
                INSERT INTO ai.saved_product_baseline
                    (user_id, product_id, baseline_price, baseline_in_stock, baseline_at)
                SELECT %s, p.id, p.price, p.in_stock, now()
                FROM public.products p
                WHERE p.id = %s
                ON CONFLICT (user_id, product_id) DO UPDATE SET
                    baseline_price = EXCLUDED.baseline_price,
                    baseline_in_stock = EXCLUDED.baseline_in_stock,
                    baseline_at = now()
                """,
                (user_id, int(body.product_id)),
            )

    if not row:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already saved")
    if body.product_id.isdigit():
        await record_product_signal(
            pool,
            user_id=user_id,
            product_id=int(body.product_id),
            signal_type="save",
            dedupe_key=f"save:{row[0]}",
        )
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
                SELECT s.save_id, s.created_at,
                       p.id, p.brand, p.name, p.price, p.image_url, p.in_stock
                FROM ai.saves s
                LEFT JOIN public.products p
                       ON p.id = CASE WHEN s.product_id ~ '^[0-9]+$' THEN s.product_id::bigint END
                WHERE s.user_id = %s
                  AND s.created_at < (SELECT created_at FROM ai.saves WHERE save_id = %s AND user_id = %s)
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (user_id, UUID(cursor), user_id, limit + 1),
            )
        else:
            await cur.execute(
                """
                SELECT s.save_id, s.created_at,
                       p.id, p.brand, p.name, p.price, p.image_url, p.in_stock
                FROM ai.saves s
                LEFT JOIN public.products p
                       ON p.id = CASE WHEN s.product_id ~ '^[0-9]+$' THEN s.product_id::bigint END
                WHERE s.user_id = %s
                ORDER BY s.created_at DESC
                LIMIT %s
                """,
                (user_id, limit + 1),
            )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1][0]) if has_more and page else None

    items: list[SaveListItem] = []
    for r in page:
        product = (
            SavedProduct(
                id=r[2],
                brand=r[3],
                name=r[4],
                price=float(r[5]) if r[5] is not None else None,
                image_url=r[6],
                in_stock=r[7],
            )
            if r[2] is not None
            else None
        )
        items.append(SaveListItem(save_id=str(r[0]), product=product, created_at=r[1].isoformat()))

    return SaveListResponse(items=items, next_cursor=next_cursor, total=total)


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
        if row and product_id.isdigit():
            await cur.execute(
                "DELETE FROM ai.saved_product_baseline WHERE user_id = %s AND product_id = %s",
                (user_id, int(product_id)),
            )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Save not found")
    if product_id.isdigit():
        await record_product_signal(
            pool,
            user_id=user_id,
            product_id=int(product_id),
            signal_type="unsave",
            dedupe_key=f"unsave:{row[0]}:{datetime.now(tz=UTC).isoformat()}",
        )
