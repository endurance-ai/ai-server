"""Notification inbox (알림함) API.

GET   /v1/notifications        — 알림 피드 (헤더 벨). keyset cursor(id DESC) + unread_count 동봉
PATCH /v1/notifications/read   — 읽음 처리 (ids 지정 또는 all)

배치(app/services/notifications.py)가 적재한 ai.notifications 를 유저 기준으로 읽는다.
문안(text/sub)은 배치가 payload 에 남긴 값을 우선 쓰고, 없으면 kind 별 KO 템플릿으로
서버에서 만든다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, model_validator

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["notifications"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class NotificationItem(BaseModel):
    id: str
    type: str  # 원본 DB kind (restock / price_drop / brand_new_product / brand_sale)
    text: str
    sub: str
    brand: str | None
    product_id: int | None
    brand_id: int | None
    old_price: int | None
    new_price: int | None
    image_url: str | None
    created_at: str
    read: bool


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
    next_cursor: str | None
    unread_count: int


class MarkReadRequest(BaseModel):
    ids: list[str] | None = None
    all: bool | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> MarkReadRequest:
        if bool(self.ids) == bool(self.all):
            raise ValueError("provide exactly one of `ids` or `all`")
        return self


class MarkReadResponse(BaseModel):
    unread_count: int
    marked: int = Field(default=0)


# ── Copy ──────────────────────────────────────────────────────────────────────


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _copy(kind: str, payload: dict[str, Any], brand: str | None, name: str | None) -> tuple[str, str]:
    """kind 별 (text, sub). payload 에 미리 채운 값이 있으면 우선한다."""
    if payload.get("text"):
        return str(payload["text"]), str(payload.get("sub") or "")

    label = brand or "브랜드"
    if kind == "price_drop":
        drop_pct = payload.get("drop_pct")
        sub = f"최대 {int(drop_pct)}% 싸요" if drop_pct else ""
        return f"찜하신 {label} 상품이 할인되었어요", sub
    if kind == "restock":
        return f"찜하신 {label} 상품이 재입고됐어요", name or ""
    if kind == "brand_new_product":
        return f"{label}에 신상이 들어왔어요", name or ""
    if kind == "brand_sale":
        # 배치는 payload 에 text/sub 를 남기지 않고 brand/max_discount_pct 등만 남기므로
        # 이 분기가 실제 렌더 경로다. 문안은 푸시(_single_copy)와 동일한 PRD 표현을 쓴다.
        max_discount_pct = payload.get("max_discount_pct")
        sub = f"최대 {int(max_discount_pct)}% 싸요" if max_discount_pct else ""
        return f"팔로우한 {label} 세일 시작했어요", sub
    return f"{label} 소식이 있어요", ""


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/notifications", response_model=NotificationListResponse, status_code=status.HTTP_200_OK)
async def list_notifications(
    cursor: str | None = Query(default=None, description="Pagination cursor (notification id)"),
    limit: int = Query(default=20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> NotificationListResponse:
    """알림 피드. 최신순(id DESC) keyset 페이지네이션 + 읽지 않은 개수 동봉."""
    cursor_id = int(cursor) if cursor and cursor.isdigit() else None

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT n.id, n.kind, n.product_id, n.brand_node_id, n.payload,
                   n.created_at, n.read_at,
                   p.brand, p.name, p.image_url,
                   bn.brand_name
            FROM ai.notifications n
            LEFT JOIN public.products p ON p.id = n.product_id
            LEFT JOIN public.brand_nodes bn ON bn.id = n.brand_node_id
            WHERE n.user_id = %s
              AND (%s::bigint IS NULL OR n.id < %s::bigint)
            ORDER BY n.id DESC
            LIMIT %s
            """,
            (user_id, cursor_id, cursor_id, limit + 1),
        )
        rows = await cur.fetchall()

        await cur.execute(
            "SELECT count(*) FROM ai.notifications WHERE user_id = %s AND read_at IS NULL",
            (user_id,),
        )
        unread_count = (await cur.fetchone())[0]

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1][0]) if has_more and page else None

    items: list[NotificationItem] = []
    for r in page:
        kind = r[1]
        payload = r[4] or {}
        brand = payload.get("brand") or r[7] or r[10]
        name = payload.get("name") or r[8]
        text, sub = _copy(kind, payload, brand, name)
        old_price = _int_or_none(payload.get("baseline_price")) if kind == "price_drop" else None
        new_price = _int_or_none(payload.get("price")) if kind == "price_drop" else None
        image_url = r[9] or None
        items.append(
            NotificationItem(
                id=str(r[0]),
                type=kind,
                text=text,
                sub=sub,
                brand=brand,
                product_id=r[2],
                brand_id=r[3],
                old_price=old_price,
                new_price=new_price,
                image_url=image_url,
                created_at=r[5].isoformat(),
                read=r[6] is not None,
            )
        )

    return NotificationListResponse(items=items, next_cursor=next_cursor, unread_count=unread_count)


@router.patch("/notifications/read", response_model=MarkReadResponse, status_code=status.HTTP_200_OK)
async def mark_read(
    body: MarkReadRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> MarkReadResponse:
    """읽음 처리. `all` 이면 미읽음 전체를, `ids` 면 해당 알림만 (유저 스코프)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        if body.all:
            await cur.execute(
                """
                UPDATE ai.notifications
                SET read_at = now()
                WHERE user_id = %s AND read_at IS NULL
                RETURNING id
                """,
                (user_id,),
            )
        else:
            ids = [int(i) for i in (body.ids or []) if str(i).isdigit()]
            await cur.execute(
                """
                UPDATE ai.notifications
                SET read_at = now()
                WHERE user_id = %s AND read_at IS NULL AND id = ANY(%s)
                RETURNING id
                """,
                (user_id, ids),
            )
        marked = len(await cur.fetchall())

        await cur.execute(
            "SELECT count(*) FROM ai.notifications WHERE user_id = %s AND read_at IS NULL",
            (user_id,),
        )
        unread_count = (await cur.fetchone())[0]

    return MarkReadResponse(unread_count=unread_count, marked=marked)
