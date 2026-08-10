"""Notification inbox (알림함) API.

GET   /v1/notifications        — 알림 피드 (헤더 벨). keyset cursor + unread_count 동봉
PATCH /v1/notifications/read   — 읽음 처리 (ids 지정 또는 all)

피드는 **두 소스를 합친 뷰**다 (migration 0027/0028).

  source `n` — ai.notifications: 찜 기반 개인 이벤트(restock/price_drop)와 brand_new.
               유저별로 이미 다른 내용이라 팬아웃할 게 없다. 행마다 read_at 을 갖는다.
  source `b` — ai.brand_news: 브랜드 단위 소식(brand_sale). 정본 1행을 팔로워가
               공유해서 읽는다(read fan-out). 공유 행이라 read_at 을 못 달고
               ai.user_feed_state 워터마크 + ai.feed_reads 예외로 읽음을 판정한다.

이 분리 덕분에 브랜드 소식은 푸시 정책과 무관하게 인박스에 보인다 — 주간 캡에 걸렸거나
동의를 꺼서 푸시가 안 나간 유저도 알림함에서는 소식을 본다. ai.notifications 의
brand_sale 행은 아웃박스 앵커(notification_message_events FK) 전용이라 피드에서 제외한다.

문안(text/sub)은 배치가 payload 에 남긴 값을 우선 쓰고, 없으면 kind 별 KO 템플릿으로
서버에서 만든다.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field, model_validator

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool

router = APIRouter(prefix="/v1", tags=["notifications"])

# 피드 아이템 id 접두사. "n:123" = ai.notifications 행, "b:45" = ai.brand_news 행.
# 두 소스의 id 공간이 겹치므로 접두사 없이는 읽음 처리 대상을 지목할 수 없다.
SOURCE_NOTIFICATION = "n"
SOURCE_BRAND_NEWS = "b"
# ai.feed_reads.source CHECK 값 (예외 테이블은 공유 행 소스만 담는다).
_FEED_READ_SOURCE = "brand_news"

# 아웃박스 앵커 전용이라 피드에서 제외하는 kind. 같은 소식을 source `b` 가 이미 낸다.
_PUSH_LEDGER_KIND = "brand_sale"

# ai.brand_news 중 알림함에 노출하는 종류. 'brand_new'(브랜드 홈 "신상 N개" 요약)는
# 뺀다 — 알림함에는 이미 상품별 brand_new_product 행이 성별까지 맞춰 들어오므로,
# 브랜드 단위 요약까지 끼면 같은 사실이 두 번 보인다. 브랜드 홈은 개인화할 대상이
# 없어서 요약이 유일한 표현이지만, 알림함은 사정이 다르다.
_INBOX_NEWS_KINDS = ("brand_sale",)


# ── Schemas ───────────────────────────────────────────────────────────────────


class NotificationItem(BaseModel):
    id: str
    """`<source>:<row id>` — 예: "n:123", "b:45". 읽음 처리에 그대로 돌려주면 된다."""
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


# ── Cursor ────────────────────────────────────────────────────────────────────
#
# 두 소스를 합치므로 단일 `id DESC` 로는 페이지 경계를 표현할 수 없다. 정렬 키는
# (created_at, source, id) 세 값 전부 DESC — 방향을 섞으면 튜플 비교가 성립하지 않는다.
# opaque base64url 토큰으로 감싸는 이유는 follows 커서와 같다: ISO 타임스탬프의 `+00:00`
# 이 쿼리스트링에서 공백으로 치환되는 문제를 원천 차단한다.


def _decode_cursor(cursor: str | None) -> tuple[str | None, str | None, int | None]:
    if not cursor:
        return None, None, None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts_part, source, id_part = raw.rsplit("|", 2)
        if source not in (SOURCE_NOTIFICATION, SOURCE_BRAND_NEWS):
            return None, None, None
        return ts_part, source, int(id_part)
    except (ValueError, UnicodeDecodeError):
        return None, None, None


def _encode_cursor(at: datetime, source: str, row_id: int) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{source}|{row_id}".encode()).decode("ascii")


def _parse_item_id(value: str) -> tuple[str, int] | None:
    """ "n:123" → ("n", 123). 형식이 어긋나면 None (조용히 무시)."""
    source, _, raw_id = value.partition(":")
    if source not in (SOURCE_NOTIFICATION, SOURCE_BRAND_NEWS) or not raw_id.isdigit():
        return None
    return source, int(raw_id)


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
        # 브랜드 홈(brand_news_copy)과 달리 "팔로우한" 을 붙인다 — 피드에서는 왜 이
        # 소식을 받는지가 맥락으로 드러나지 않는다.
        max_discount_pct = payload.get("max_discount_pct")
        sub = f"최대 {int(max_discount_pct)}% 싸요" if max_discount_pct else ""
        return f"팔로우한 {label} 세일 시작했어요", sub
    return f"{label} 소식이 있어요", ""


# ── Queries ───────────────────────────────────────────────────────────────────

# 두 소스를 각각 LIMIT 으로 자른 뒤 합쳐 다시 자른다. 각 서브쿼리가 자기 인덱스
# (idx_notifications_user_feed / idx_brand_news_feed)로 조기 종료하므로, 팔로우 수가
# 늘어도 전체 스캔이 되지 않는다.
#
# source `b` 의 `bn.started_at >= ubp.created_at` — 팔로우 이전 소식은 보여주지 않는다.
# write fan-out 시절엔 백필이 없어 자연히 그랬고, read fan-out 이 되었다고 갑자기 과거
# 소식이 쏟아지면 유저에겐 버그로 보인다. notify_enabled 로 게이트하는 것도 같은 이유로
# 기존 팬아웃 조건(_BRAND_FOLLOWERS_SQL)을 그대로 따른 것이다.
_FEED_SQL = f"""
    WITH feed AS (
        (
            SELECT '{SOURCE_NOTIFICATION}'::text AS source,
                   n.id, n.created_at AS at, n.kind, n.product_id, n.brand_node_id,
                   n.payload, (n.read_at IS NOT NULL) AS read
            FROM ai.notifications n
            WHERE n.user_id = %(uid)s
              AND n.kind <> '{_PUSH_LEDGER_KIND}'
              AND (
                  %(cur_at)s::timestamptz IS NULL
                  OR (n.created_at, '{SOURCE_NOTIFICATION}', n.id)
                     < (%(cur_at)s::timestamptz, %(cur_source)s::text, %(cur_id)s::bigint)
              )
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT %(lim)s
        )
        UNION ALL
        (
            SELECT '{SOURCE_BRAND_NEWS}'::text AS source,
                   bn.id, bn.started_at AS at, bn.kind, NULL::bigint, bn.brand_node_id,
                   bn.payload,
                   (bn.started_at <= coalesce(ufs.last_read_at, '-infinity'::timestamptz)
                    OR fr.ref_id IS NOT NULL) AS read
            FROM ai.brand_news bn
            JOIN ai.user_brand_picks ubp
              ON ubp.brand_id = bn.brand_node_id AND ubp.user_id = %(uid)s AND ubp.notify_enabled
            LEFT JOIN ai.user_feed_state ufs ON ufs.user_id = %(uid)s
            LEFT JOIN ai.feed_reads fr
              ON fr.user_id = %(uid)s AND fr.source = '{_FEED_READ_SOURCE}' AND fr.ref_id = bn.id
            WHERE bn.kind = ANY(%(news_kinds)s)
              AND bn.started_at >= ubp.created_at
              AND (
                  %(cur_at)s::timestamptz IS NULL
                  OR (bn.started_at, '{SOURCE_BRAND_NEWS}', bn.id)
                     < (%(cur_at)s::timestamptz, %(cur_source)s::text, %(cur_id)s::bigint)
              )
            ORDER BY bn.started_at DESC, bn.id DESC
            LIMIT %(lim)s
        )
    )
    SELECT f.source, f.id, f.at, f.kind, f.product_id, f.brand_node_id, f.payload, f.read,
           p.brand, p.name, p.image_url, bnode.brand_name
    FROM feed f
    LEFT JOIN public.products p ON p.id = f.product_id
    LEFT JOIN public.brand_nodes bnode ON bnode.id = f.brand_node_id
    ORDER BY f.at DESC, f.source DESC, f.id DESC
    LIMIT %(lim)s
"""

_UNREAD_SQL = f"""
    SELECT (
        SELECT count(*) FROM ai.notifications
        WHERE user_id = %(uid)s AND kind <> '{_PUSH_LEDGER_KIND}' AND read_at IS NULL
    ) + (
        SELECT count(*)
        FROM ai.brand_news bn
        JOIN ai.user_brand_picks ubp
          ON ubp.brand_id = bn.brand_node_id AND ubp.user_id = %(uid)s AND ubp.notify_enabled
        LEFT JOIN ai.feed_reads fr
          ON fr.user_id = %(uid)s AND fr.source = '{_FEED_READ_SOURCE}' AND fr.ref_id = bn.id
        WHERE bn.kind = ANY(%(news_kinds)s)
          AND bn.started_at >= ubp.created_at
          AND bn.started_at > coalesce(
              (SELECT last_read_at FROM ai.user_feed_state WHERE user_id = %(uid)s),
              '-infinity'::timestamptz
          )
          AND fr.ref_id IS NULL
    )
"""


async def _unread_count(cur: Any, user_id: UUID) -> int:
    await cur.execute(_UNREAD_SQL, {"uid": user_id, "news_kinds": list(_INBOX_NEWS_KINDS)})
    return (await cur.fetchone())[0]


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/notifications", response_model=NotificationListResponse, status_code=status.HTTP_200_OK)
async def list_notifications(
    cursor: str | None = Query(default=None, description="Pagination cursor (opaque token)"),
    limit: int = Query(default=20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> NotificationListResponse:
    """알림 피드. 최신순 keyset 페이지네이션 + 읽지 않은 개수 동봉."""
    cursor_at, cursor_source, cursor_id = _decode_cursor(cursor)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _FEED_SQL,
            {
                "uid": user_id,
                "cur_at": cursor_at,
                "cur_source": cursor_source,
                "cur_id": cursor_id,
                "news_kinds": list(_INBOX_NEWS_KINDS),
                "lim": limit + 1,
            },
        )
        rows = await cur.fetchall()
        unread_count = await _unread_count(cur, user_id)

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = _encode_cursor(page[-1][2], page[-1][0], page[-1][1]) if has_more and page else None

    items: list[NotificationItem] = []
    for r in page:
        source, row_id, at, kind = r[0], r[1], r[2], r[3]
        payload = r[6] or {}
        brand = payload.get("brand") or r[8] or r[11]
        name = payload.get("name") or r[9]
        text, sub = _copy(kind, payload, brand, name)
        items.append(
            NotificationItem(
                id=f"{source}:{row_id}",
                type=kind,
                text=text,
                sub=sub,
                brand=brand,
                product_id=r[4],
                brand_id=r[5],
                old_price=_int_or_none(payload.get("baseline_price")) if kind == "price_drop" else None,
                new_price=_int_or_none(payload.get("price")) if kind == "price_drop" else None,
                image_url=r[10] or None,
                created_at=at.isoformat(),
                read=r[7],
            )
        )

    return NotificationListResponse(items=items, next_cursor=next_cursor, unread_count=unread_count)


@router.patch("/notifications/read", response_model=MarkReadResponse, status_code=status.HTTP_200_OK)
async def mark_read(
    body: MarkReadRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> MarkReadResponse:
    """읽음 처리. `all` 이면 미읽음 전체를, `ids` 면 해당 항목만 (유저 스코프).

    두 소스의 읽음 저장 위치가 다르므로 처리도 갈린다 — 개인 이벤트는 행의 read_at,
    브랜드 소식은 워터마크(전체) 또는 예외 테이블(개별).
    """
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        if body.all:
            marked = await _mark_all_read(cur, user_id)
        else:
            marked = await _mark_ids_read(cur, user_id, body.ids or [])
        unread_count = await _unread_count(cur, user_id)

    return MarkReadResponse(unread_count=unread_count, marked=marked)


async def _mark_all_read(cur: Any, user_id: UUID) -> int:
    """미읽음 전체. 브랜드 소식 쪽은 워터마크 1행 UPDATE 로 끝난다."""
    # 워터마크를 올리기 전에 세어야 '몇 건을 읽음 처리했는지' 가 맞는다.
    unread_before = await _unread_count(cur, user_id)

    await cur.execute(
        "UPDATE ai.notifications SET read_at = now() WHERE user_id = %s AND read_at IS NULL",
        (user_id,),
    )
    await cur.execute(
        """
        INSERT INTO ai.user_feed_state (user_id, last_read_at, updated_at)
        VALUES (%s, now(), now())
        ON CONFLICT (user_id) DO UPDATE SET last_read_at = now(), updated_at = now()
        """,
        (user_id,),
    )
    # 워터마크가 전부 덮으므로 예외 행은 이제 중복이다. 무한히 쌓이지 않게 정리한다.
    await cur.execute("DELETE FROM ai.feed_reads WHERE user_id = %s", (user_id,))
    return unread_before


async def _mark_ids_read(cur: Any, user_id: UUID, raw_ids: list[str]) -> int:
    parsed = [p for p in (_parse_item_id(str(i)) for i in raw_ids) if p is not None]
    notification_ids = [row_id for source, row_id in parsed if source == SOURCE_NOTIFICATION]
    news_ids = [row_id for source, row_id in parsed if source == SOURCE_BRAND_NEWS]

    marked = 0
    if notification_ids:
        await cur.execute(
            """
            UPDATE ai.notifications SET read_at = now()
            WHERE user_id = %s AND read_at IS NULL AND id = ANY(%s)
            RETURNING id
            """,
            (user_id, notification_ids),
        )
        marked += len(await cur.fetchall())

    if news_ids:
        # 팔로우하지 않은 브랜드의 소식을 읽음 처리하려는 시도는 조용히 무시한다
        # (INSERT ... SELECT 의 EXISTS 가 소유권 검사를 겸한다).
        await cur.execute(
            f"""
            INSERT INTO ai.feed_reads (user_id, source, ref_id)
            SELECT %(uid)s, '{_FEED_READ_SOURCE}', bn.id
            FROM ai.brand_news bn
            JOIN ai.user_brand_picks ubp
              ON ubp.brand_id = bn.brand_node_id AND ubp.user_id = %(uid)s AND ubp.notify_enabled
            WHERE bn.id = ANY(%(ids)s)
              AND bn.started_at > coalesce(
                  (SELECT last_read_at FROM ai.user_feed_state WHERE user_id = %(uid)s),
                  '-infinity'::timestamptz
              )
            ON CONFLICT DO NOTHING
            RETURNING ref_id
            """,
            {"uid": user_id, "ids": news_ids},
        )
        marked += len(await cur.fetchall())

    return marked
