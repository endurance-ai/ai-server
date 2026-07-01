"""Unified history feed API.

GET /v1/history — time-ordered unified feed of result sets (is_listed searches)
                  + viewed products (PDP) within a single session.

The feed merges ai.searches and ai.product_views by occurred_at DESC. `type`
filters to one source. Cursor is an opaque base64(occurred_at|id) token because
two sources are merged (a single id can't order the feed).
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["history"])


# ── Schemas ───────────────────────────────────────────────────────────────────


class HistoryItem(BaseModel):
    type: Literal["result_set", "product"]
    occurred_at: str
    # result_set fields
    search_id: str | None = None
    title: str | None = None
    result_count: int | None = None
    preview_images: list[str] | None = None
    # product fields
    product_id: int | None = None
    brand: str | None = None
    name: str | None = None
    price: float | None = None
    image_url: str | None = None
    product_url: str | None = None
    source_search_id: str | None = None


class HistoryResponse(BaseModel):
    items: list[HistoryItem]
    next_cursor: str | None


# ── Cursor helpers ────────────────────────────────────────────────────────────


def _encode_cursor(occurred_at: datetime, ident: str) -> str:
    return base64.urlsafe_b64encode(f"{occurred_at.isoformat()}|{ident}".encode()).decode()


def _decode_cursor(cursor: str) -> datetime:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    ts, _ident = raw.split("|", 1)
    return datetime.fromisoformat(ts)


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get("/history", response_model=HistoryResponse, status_code=status.HTTP_200_OK)
async def list_history(
    session_id: UUID | None = Query(default=None, description="세션 ID (미전달 시 전체 세션)"),
    feed_type: Literal["all", "result_set", "product"] = Query(default="all", alias="type"),
    cursor: str | None = Query(default=None, description="Opaque pagination cursor"),
    limit: int = Query(default=20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> HistoryResponse:
    """결과셋 + PDP 조회를 시간순 통합 피드로 반환. session_id 미전달 시 전체 세션 통합."""
    before_ts: datetime | None = None
    if cursor:
        try:
            before_ts = _decode_cursor(cursor)
        except Exception:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid cursor") from None

    include_rs = feed_type in ("all", "result_set")
    include_pv = feed_type in ("all", "product")

    union_parts: list[str] = []
    params: list = []
    if include_rs:
        if session_id is not None:
            union_parts.append(
                """
                SELECT 'result_set' AS kind, s.created_at AS occurred_at, s.search_id::text AS ident
                FROM ai.searches s
                WHERE s.session_id = %s AND s.user_id = %s AND s.is_listed = TRUE
                """
            )
            params += [session_id, user_id]
        else:
            union_parts.append(
                """
                SELECT 'result_set' AS kind, s.created_at AS occurred_at, s.search_id::text AS ident
                FROM ai.searches s
                WHERE s.user_id = %s AND s.is_listed = TRUE
                """
            )
            params += [user_id]
    if include_pv:
        if session_id is not None:
            union_parts.append(
                """
                SELECT 'product' AS kind, pv.viewed_at AS occurred_at, pv.view_id::text AS ident
                FROM ai.product_views pv
                WHERE pv.session_id = %s AND pv.user_id = %s
                """
            )
            params += [session_id, user_id]
        else:
            union_parts.append(
                """
                SELECT 'product' AS kind, pv.viewed_at AS occurred_at, pv.view_id::text AS ident
                FROM ai.product_views pv
                WHERE pv.user_id = %s
                """
            )
            params += [user_id]

    union_sql = " UNION ALL ".join(f"({p})" for p in union_parts)
    where_cursor = ""
    if before_ts is not None:
        where_cursor = "WHERE occurred_at < %s"
        params.append(before_ts)
    feed_sql = f"""
        SELECT kind, occurred_at, ident FROM ({union_sql}) feed
        {where_cursor}
        ORDER BY occurred_at DESC
        LIMIT %s
    """
    params.append(limit + 1)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(feed_sql, params)
        feed = await cur.fetchall()

        has_more = len(feed) > limit
        page = feed[:limit]
        if not page:
            return HistoryResponse(items=[], next_cursor=None)

        rs_ids = [UUID(r[2]) for r in page if r[0] == "result_set"]
        pv_ids = [UUID(r[2]) for r in page if r[0] == "product"]

        rs_map: dict[str, dict] = {}
        pv_map: dict[str, dict] = {}
        if rs_ids:
            await cur.execute(
                """
                SELECT s.search_id::text, s.title, s.total, s.cover_image_urls
                FROM ai.searches s
                WHERE s.search_id = ANY(%s)
                """,
                (rs_ids,),
            )
            for sr in await cur.fetchall():
                rs_map[sr[0]] = {
                    "title": sr[1],
                    "result_count": sr[2],
                    "preview_images": list(sr[3]) if sr[3] else [],
                }
        if pv_ids:
            await cur.execute(
                """
                SELECT pv.view_id::text, pv.product_id, p.brand, p.name, p.price,
                       p.image_url, p.product_url, pv.source_search_id
                FROM ai.product_views pv
                JOIN public.products p ON p.id = pv.product_id
                WHERE pv.view_id = ANY(%s)
                """,
                (pv_ids,),
            )
            for vr in await cur.fetchall():
                pv_map[vr[0]] = {
                    "product_id": vr[1],
                    "brand": vr[2],
                    "name": vr[3],
                    "price": float(vr[4]) if vr[4] is not None else None,
                    "image_url": vr[5],
                    "product_url": vr[6],
                    "source_search_id": str(vr[7]) if vr[7] else None,
                }

    items: list[HistoryItem] = []
    for kind, occurred_at, ident in page:
        ts = occurred_at.isoformat()
        if kind == "result_set":
            data = rs_map.get(ident)
            if data is not None:
                items.append(HistoryItem(type="result_set", occurred_at=ts, search_id=ident, **data))
        else:
            data = pv_map.get(ident)
            if data is not None:
                items.append(HistoryItem(type="product", occurred_at=ts, **data))

    last = page[-1]
    next_cursor = _encode_cursor(last[1], last[2]) if has_more else None
    return HistoryResponse(items=items, next_cursor=next_cursor)
