"""알림 1차 — 재입고 · 가격 하락 · 관심 브랜드 신규 상품.

재수집 배치가 상품의 가격·재고를 매일 갱신하게 된 뒤, 그 갱신을 유저에게 전달하는
첫 기능이다. 세 종류 모두 ai-server 가 **자기 스냅샷으로** 감지한다 — 크롤러는
건드리지 않는다.

| 종류 | 트리거 | 구독 모델 |
|---|---|---|
| restock            | 찜한 상품이 품절 → 판매중                | 찜 = 구독 |
| price_drop         | 찜한 상품이 기준가 대비 임계치 이상 하락 | 찜 = 구독 |
| brand_new_product  | 관심 브랜드에 새 상품 등장               | 온보딩 브랜드 선택 = 구독 |

기준가 의미가 스냅샷 방식의 핵심이다. 크롤러 이벤트는 *직전 크롤 대비* 델타라
하루 3%씩 5일 내리면 어떤 단일 이벤트도 15%를 넘지 않아 놓친다. 여기서는
**찜한 시점(또는 마지막 알림 시점) 기준가**와 비교하므로 "네가 찜한 뒤 15% 내렸다"
가 그대로 성립한다. 대상도 전 상품이 아니라 찜한 상품만이라 훨씬 작다.

발송 정책: 유저당 **일 1건 다이제스트**, 브랜드 신규 상품은 **주 N일 캡**.
둘 다 `ai.notifications` 집계로 판정한다 (migration 0023).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

from app.core.config import settings
from app.services.curation_refresh import GENDER_MATCH_SQL, PRODUCT_FEATURES_JOIN
from app.services.push import apns

logger = logging.getLogger(__name__)

KIND_RESTOCK = "restock"
KIND_PRICE_DROP = "price_drop"
KIND_BRAND_NEW = "brand_new_product"
KINDS: tuple[str, ...] = (KIND_RESTOCK, KIND_PRICE_DROP, KIND_BRAND_NEW)

# 마지막 실행 시각을 남기는 ai.notification_job_state 행 키 (관측용 — 감지는 이 값을
# 읽지 않는다. 이유는 _NEW_PRODUCT_SQL 주석 참조).
NOTIFY_JOB = "notify_batch"
SAVED_JOB = "notify_saved_detect"
BRAND_JOB = "notify_brand_detect"
SAVED_CATEGORY = "saved_product_digest"
BRAND_CATEGORY = "brand_new_digest"

# 알림 종류는 기본 opt-in. 유저가 끄면 notification_settings 에 false 가 들어온다.
DEFAULT_KIND_CONSENT = True


# ── 도메인 타입 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SavedRow:
    """찜 ⋈ 상품 ⋈ 기준가 한 행."""

    user_id: UUID
    product_id: int
    brand: str | None
    name: str | None
    price: float | None
    in_stock: bool | None
    has_baseline: bool
    baseline_price: float | None
    baseline_in_stock: bool | None


@dataclass(frozen=True, slots=True)
class NewProductRow:
    user_id: UUID
    product_id: int
    brand_node_id: int | None
    brand: str | None
    name: str | None
    price: float | None


@dataclass(frozen=True, slots=True)
class Event:
    user_id: UUID
    kind: str
    product_id: int
    brand_node_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BaselineWrite:
    user_id: UUID
    product_id: int
    price: float | None
    in_stock: bool | None


@dataclass(frozen=True, slots=True)
class Digest:
    title: str
    body: str
    data: dict[str, Any]


@dataclass
class BatchReport:
    detected: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    selected: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS, 0))
    baselines_written: int = 0
    recorded: int = 0
    messages_queued: int = 0
    deliveries_queued: int = 0
    users_pushed: int = 0
    pushes_ok: int = 0
    pushes_failed: int = 0
    dead_tokens_dropped: int = 0
    dry_run: bool = False
    aborted: str = ""

    def as_line(self) -> str:
        detected = " ".join(f"{k}={v}" for k, v in self.detected.items())
        selected = " ".join(f"{k}={v}" for k, v in self.selected.items())
        prefix = "DRY-RUN " if self.dry_run else ""
        if self.aborted:
            prefix = f"ABORTED({self.aborted}) "
        return (
            f"🔔 [notify] {prefix}"
            f"detected({detected}) selected({selected}) "
            f"baselines={self.baselines_written} recorded={self.recorded} "
            f"messages={self.messages_queued} deliveries={self.deliveries_queued} "
            f"users_pushed={self.users_pushed} ok={self.pushes_ok} failed={self.pushes_failed} "
            f"dead_tokens={self.dead_tokens_dropped}"
        )


# ── 순수 판정 ────────────────────────────────────────────────────────────────


def detect_save_events(
    rows: list[SavedRow],
    *,
    threshold: float,
) -> tuple[list[Event], list[BaselineWrite]]:
    """찜 기반 두 종류를 판정하고, 함께 갱신할 기준값을 돌려준다.

    기준가 정책:
      - 기준이 없는 찜(기존 62건 포함)은 현재값으로 **백필만** 하고 알림하지 않는다.
      - 가격은 하락 알림이 나갈 때만 현재가로 내린다. 값이 올랐다고 기준을 올리면
        "올랐다가 제자리로" 를 하락으로 오인한다.
      - 재고는 항상 현재값을 따라간다. 품절을 기록해 두지 않으면 다음 재입고를
        영영 감지할 수 없다.
    """
    events: list[Event] = []
    baselines: list[BaselineWrite] = []

    for row in rows:
        if not row.has_baseline:
            baselines.append(BaselineWrite(row.user_id, row.product_id, row.price, row.in_stock))
            continue

        fired_drop = False
        if row.in_stock and row.baseline_in_stock is False:
            events.append(
                Event(
                    user_id=row.user_id,
                    kind=KIND_RESTOCK,
                    product_id=row.product_id,
                    payload=_product_payload(row.brand, row.name, row.price),
                )
            )

        drop_ratio = _drop_ratio(row.baseline_price, row.price)
        if drop_ratio is not None and drop_ratio >= threshold:
            fired_drop = True
            events.append(
                Event(
                    user_id=row.user_id,
                    kind=KIND_PRICE_DROP,
                    product_id=row.product_id,
                    payload={
                        **_product_payload(row.brand, row.name, row.price),
                        "baseline_price": row.baseline_price,
                        "drop_pct": round(drop_ratio * 100),
                    },
                )
            )

        stock_changed = row.in_stock is not None and row.in_stock != row.baseline_in_stock
        if fired_drop or stock_changed:
            baselines.append(
                BaselineWrite(
                    row.user_id,
                    row.product_id,
                    row.price if fired_drop else row.baseline_price,
                    row.in_stock,
                )
            )

    return events, baselines


def _drop_ratio(baseline_price: float | None, price: float | None) -> float | None:
    if not baseline_price or baseline_price <= 0 or price is None or price < 0:
        return None
    if price >= baseline_price:
        return None
    return (baseline_price - price) / baseline_price


def _product_payload(brand: str | None, name: str | None, price: float | None) -> dict[str, Any]:
    return {"brand": brand, "name": name, "price": price}


def new_product_events(rows: list[NewProductRow]) -> list[Event]:
    return [
        Event(
            user_id=row.user_id,
            kind=KIND_BRAND_NEW,
            product_id=row.product_id,
            brand_node_id=row.brand_node_id,
            payload=_product_payload(row.brand, row.name, row.price),
        )
        for row in rows
    ]


def kind_allowed(prefs: dict[str, Any] | None, kind: str) -> bool:
    """Apply the existing mobile master/marketing controls plus kind opt-out."""
    values = prefs or {}
    if not bool(values.get("system", True)):
        return False
    if kind == KIND_BRAND_NEW and not bool(values.get("release_alerts", True)):
        return False
    return bool(values.get(kind, DEFAULT_KIND_CONSENT))


def select_for_delivery(
    events: list[Event],
    *,
    prefs: dict[UUID, dict[str, Any] | None],
    brand_new_days_last_week: dict[UUID, int],
    weekly_cap: int,
    max_brand_items: int,
) -> dict[UUID, list[Event]]:
    """동의 · 주간 캡 · 다이제스트 크기를 적용해 유저별 발송 목록을 만든다.

    브랜드 신규 상품은 최근 7일 중 알림이 나간 **날짜 수**로 캡한다. 하루에 몇 개를
    묶어 보내든 한 번으로 세야 다이제스트 정책과 앞뒤가 맞는다.
    """
    per_user: dict[UUID, list[Event]] = {}
    for event in events:
        if not kind_allowed(prefs.get(event.user_id), event.kind):
            continue
        if event.kind == KIND_BRAND_NEW and brand_new_days_last_week.get(event.user_id, 0) >= weekly_cap:
            continue
        per_user.setdefault(event.user_id, []).append(event)

    ordered: dict[UUID, list[Event]] = {}
    for user_id, user_events in per_user.items():
        brand_new = [e for e in user_events if e.kind == KIND_BRAND_NEW][:max_brand_items]
        selected = [
            *[e for e in user_events if e.kind == KIND_RESTOCK],
            *[e for e in user_events if e.kind == KIND_PRICE_DROP],
            *brand_new,
        ]
        if selected:
            ordered[user_id] = selected
    return ordered


def build_digest(events: list[Event], *, notification_ids: list[int] | None = None) -> Digest:
    """유저 한 명분 알림을 푸시 한 건으로 묶는다."""
    if not events:
        raise ValueError("build_digest requires at least one event")

    kinds = {e.kind for e in events}
    if kinds == {KIND_BRAND_NEW}:
        brands = {e.payload.get("brand") for e in events if e.payload.get("brand")}
        brand_label = next(iter(brands)) if len(brands) == 1 else "관심 브랜드"
        title, body = f"{brand_label}에 신상 {len(events)}개가 들어왔어요", ""
    elif len(events) == 1:
        title, body = _single_copy(events[0])
    else:
        title, body = f"찜한 상품 {len(events)}개에 소식이 있어요", _headline(events[0])

    return Digest(
        title=title,
        body=body,
        data={
            "kind": events[0].kind,
            "product_ids": [e.product_id for e in events],
            "notification_ids": notification_ids or [],
        },
    )


def _single_copy(event: Event) -> tuple[str, str]:
    """찜 알림(가격 하락 · 재입고) 한 건짜리 문구. PRD 알림 문안 표 참조."""
    if event.kind == KIND_PRICE_DROP:
        brand = event.payload.get("brand")
        title = f"찜하신 {brand} 상품이 할인되었어요" if brand else "찜한 상품이 할인되었어요"
        body = _price_range(event.payload.get("baseline_price"), event.payload.get("price"))
        return title, body
    name = event.payload.get("name") or "상품"
    return f"찜한 {name}이 재입고되었어요", ""


def _price_range(baseline: float | None, price: float | None) -> str:
    if baseline is None or price is None:
        return ""
    return f"{round(baseline):,}원 → {round(price):,}원"


def _headline(event: Event) -> str:
    label = " ".join(p for p in (event.payload.get("brand"), event.payload.get("name")) if p) or "찜한 상품"
    if event.kind == KIND_RESTOCK:
        return f"{label} 다시 입고됐어요"
    if event.kind == KIND_PRICE_DROP:
        return f"{label} 가격이 {event.payload.get('drop_pct', 0)}% 내려갔어요"
    return f"{label} 새로 들어왔어요"


# ── DB 조회 ──────────────────────────────────────────────────────────────────

# ai.saves.product_id 는 text 라 캐스팅이 필요하다 (app/api/saves.py 와 같은 패턴).
_SAVED_ROWS_SQL = """
    SELECT s.user_id, p.id, p.brand, p.name, p.price, p.in_stock,
           b.user_id IS NOT NULL, b.baseline_price, b.baseline_in_stock
    FROM ai.saves s
    JOIN ai.user_profiles up ON up.user_id = s.user_id
    JOIN public.products p
      ON p.id = CASE WHEN s.product_id ~ '^[0-9]+$' THEN s.product_id::bigint END
    LEFT JOIN ai.saved_product_baseline b
      ON b.user_id = s.user_id AND b.product_id = p.id
    WHERE (%(only_user)s::uuid IS NULL OR s.user_id = %(only_user)s::uuid)
"""

# 성별 정합은 큐레이션의 3단 다리를 그대로 쓴다 — 푸시는 검색보다 큐레이션에 가깝다
# (fail-closed: 성별을 확정할 수 없는 상품/유저는 조용히 제외). 큐레이션은 gender 를
# 배치 파라미터로 받지만 여기선 유저마다 달라 컬럼 참조로 바꾼다.
_GENDER_MATCH_PER_USER = GENDER_MATCH_SQL.replace("%(gender)s", "k.gender_app")

# 워터마크가 아니라 **보존창 + 안티조인**이다. 워터마크(마지막 스캔 시각 이후만
# 조회)를 쓰면 성별이 아직 안 붙은 신상을 지나친 뒤 워터마크가 전진해 버려서, 나중에
# VLM 이 성별을 채워도 다시는 후보가 되지 않는다. 크롤러가 gender 를 더 이상 쓰지
# 않으므로(성별은 VLM 단일 출처) 신상은 **항상** 잠시 성별 미해결 상태를 거친다 —
# 워터마크 방식으로는 구조적으로 거의 아무것도 못 잡는다.
#
# 대신 최근 N일을 매번 훑고 이미 알림이 나간 (user, product) 를 제외한다. 그래서
#   ① 상품은 성별이 해결되는 시점에 자동으로 후보가 되고,
#   ② 캡/상한에 걸려 못 나간 상품도 창이 살아 있는 동안 다음 회차에 다시 후보가 된다.
# 안티조인은 날짜와 무관하다 — brand_new_product 는 (user, product) 당 평생 1회다.
_NEW_PRODUCT_SQL = f"""
    WITH picks AS (
        SELECT ubp.user_id,
               ubp.brand_id,
               CASE up.gender WHEN 'female' THEN 'women' WHEN 'male' THEN 'men' END AS gender_app
        FROM ai.user_brand_picks ubp
        JOIN ai.user_profiles up ON up.user_id = ubp.user_id
        WHERE up.gender IN ('female', 'male')
          AND (%(only_user)s::uuid IS NULL OR ubp.user_id = %(only_user)s::uuid)
    )
    SELECT k.user_id, p.id, p.brand_node_id, p.brand, p.name, p.price
    FROM public.products p
    JOIN picks k ON k.brand_id = p.brand_node_id
    {PRODUCT_FEATURES_JOIN}
    WHERE p.created_at > %(since)s
      AND p.created_at <= %(cutoff)s
      AND p.in_stock
      AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
      AND {_GENDER_MATCH_PER_USER}
      AND NOT EXISTS (
          SELECT 1 FROM ai.notifications n
          WHERE n.user_id = k.user_id
            AND n.kind = '{KIND_BRAND_NEW}'
            AND n.product_id = p.id
      )
    ORDER BY p.created_at DESC, p.id DESC
"""


async def fetch_saved_rows(pool: AsyncConnectionPool, *, only_user: UUID | None = None) -> list[SavedRow]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_SAVED_ROWS_SQL, {"only_user": only_user})
        rows = await cur.fetchall()
    return [
        SavedRow(
            user_id=r[0],
            product_id=int(r[1]),
            brand=r[2],
            name=r[3],
            price=float(r[4]) if r[4] is not None else None,
            in_stock=r[5],
            has_baseline=bool(r[6]),
            baseline_price=float(r[7]) if r[7] is not None else None,
            baseline_in_stock=r[8],
        )
        for r in rows
    ]


async def fetch_new_product_rows(
    pool: AsyncConnectionPool,
    *,
    since: datetime,
    cutoff: datetime,
    only_user: UUID | None = None,
) -> list[NewProductRow]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _NEW_PRODUCT_SQL,
            {"since": since, "cutoff": cutoff, "only_user": only_user},
        )
        rows = await cur.fetchall()
    return [
        NewProductRow(
            user_id=r[0],
            product_id=int(r[1]),
            brand_node_id=int(r[2]) if r[2] is not None else None,
            brand=r[3],
            name=r[4],
            price=float(r[5]) if r[5] is not None else None,
        )
        for r in rows
    ]


async def record_run(pool: AsyncConnectionPool, job: str, ran_at: datetime) -> None:
    """마지막 실행 시각 기록. 감지에는 쓰지 않는다 (보존창 + 안티조인이 대신한다).

    타이머로 도는 배치라 대시보드가 없다 — `SELECT * FROM ai.notification_job_state`
    가 "배치가 살아 있는지" 를 확인하는 유일한 지점이다.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.notification_job_state
                (job, watermark, last_started_at, last_succeeded_at, heartbeat_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (job) DO UPDATE SET
                watermark = EXCLUDED.watermark,
                last_started_at = EXCLUDED.last_started_at,
                last_succeeded_at = EXCLUDED.last_succeeded_at,
                heartbeat_at = EXCLUDED.heartbeat_at,
                last_error = NULL,
                updated_at = now()
            """,
            (job, ran_at, ran_at, ran_at, ran_at),
        )
        await conn.commit()


async def fetch_preferences(pool: AsyncConnectionPool, user_ids: list[UUID]) -> dict[UUID, dict[str, Any] | None]:
    if not user_ids:
        return {}
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT user_id, notification_settings FROM ai.user_profiles WHERE user_id = ANY(%s)",
            (user_ids,),
        )
        rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


async def fetch_brand_new_days_last_week(pool: AsyncConnectionPool, user_ids: list[UUID]) -> dict[UUID, int]:
    if not user_ids:
        return {}
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT user_id, count(DISTINCT scheduled_on)
            FROM ai.notification_messages
            WHERE category = %s
              AND user_id = ANY(%s)
              AND scheduled_on >= (now() AT TIME ZONE %s)::date - 6
              AND status IN ('accepted', 'partial')
            GROUP BY user_id
            """,
            (BRAND_CATEGORY, user_ids, settings.NOTIFY_TIMEZONE),
        )
        rows = await cur.fetchall()
    return {r[0]: int(r[1]) for r in rows}


# ── Durable outbox ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RecordedEvent:
    notification_id: int
    event: Event


@dataclass(frozen=True, slots=True)
class DeliveryTask:
    delivery_id: UUID
    message_id: UUID
    device_id: UUID
    push_token: str
    environment: apns.PushEnvironment
    registered_at: datetime
    category: str
    scheduled_on: date
    expires_at: datetime
    title: str
    body: str
    payload: dict[str, Any]
    apns_id: UUID
    attempt_count: int


@dataclass(slots=True)
class DeliveryReport:
    claimed: int = 0
    accepted: int = 0
    retried: int = 0
    failed: int = 0
    expired: int = 0
    endpoints_disabled: int = 0


def _parse_clock(value: str) -> clock_time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return clock_time(hour=hour, minute=minute)


def delivery_window(category: str, now: datetime) -> tuple[date, datetime, datetime]:
    """Return the KST delivery date, due time, and same-day quiet-hour expiry."""
    tz = ZoneInfo(settings.NOTIFY_TIMEZONE)
    local = now.astimezone(tz)
    send_clock = _parse_clock(
        settings.NOTIFY_SAVED_SEND_TIME if category == SAVED_CATEGORY else settings.NOTIFY_BRAND_SEND_TIME
    )
    target = datetime.combine(local.date(), send_clock, tzinfo=tz)
    quiet_end = datetime.combine(
        local.date(),
        clock_time(settings.NOTIFY_QUIET_END_HOUR),
        tzinfo=tz,
    )
    if target < quiet_end:
        target = quiet_end
    quiet_start = datetime.combine(
        local.date(),
        clock_time(settings.NOTIFY_QUIET_START_HOUR),
        tzinfo=tz,
    )
    if local >= quiet_start:
        target = datetime.combine(local.date() + timedelta(days=1), send_clock, tzinfo=tz)
    elif local > target:
        # A delayed worker catches up during active hours rather than silently
        # moving an already-detected event to tomorrow.
        target = local
    expires = datetime.combine(
        target.date(),
        clock_time(settings.NOTIFY_QUIET_START_HOUR),
        tzinfo=tz,
    )
    return target.date(), target.astimezone(UTC), expires.astimezone(UTC)


def _message_payload(message_id: UUID, category: str, events: list[Event]) -> dict[str, Any]:
    primary = events[0]
    if len(events) == 1:
        route = f"/product/{primary.product_id}"
    elif category == SAVED_CATEGORY:
        route = "/wishlist"
    else:
        route = "/home"
    return {
        "schema_version": 1,
        "message_id": str(message_id),
        "kind": category,
        "primary_product_id": str(primary.product_id),
        "item_count": len(events),
        "route": route,
    }


def _category_for(event: Event) -> str:
    return BRAND_CATEGORY if event.kind == KIND_BRAND_NEW else SAVED_CATEGORY


async def _persist_outbox(
    pool: AsyncConnectionPool,
    *,
    selected: dict[UUID, list[Event]],
    baselines: list[BaselineWrite],
    now: datetime,
) -> tuple[int, int, int, int]:
    """Atomically advance baselines and create events, messages, deliveries."""
    recorded = 0
    messages = 0
    deliveries = 0
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        for baseline in baselines:
            await cur.execute(
                """
                INSERT INTO ai.saved_product_baseline
                    (user_id, product_id, baseline_price, baseline_in_stock, baseline_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (user_id, product_id) DO UPDATE SET
                    baseline_price = EXCLUDED.baseline_price,
                    baseline_in_stock = EXCLUDED.baseline_in_stock,
                    baseline_at = now()
                """,
                (baseline.user_id, baseline.product_id, baseline.price, baseline.in_stock),
            )

        for user_id, user_events in selected.items():
            by_category: dict[str, list[Event]] = {}
            for event in user_events:
                by_category.setdefault(_category_for(event), []).append(event)

            for category, category_events in by_category.items():
                scheduled_on, scheduled_at, expires_at = delivery_window(category, now)
                # Serialize only this user's category/day. The unique key is the
                # final guard; this lock also lets manual canaries safely race a
                # scheduled detector without aborting the whole transaction.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"notify:{user_id}:{category}:{scheduled_on.isoformat()}",),
                )
                await cur.execute(
                    """
                    SELECT message_id, status, scheduled_at
                    FROM ai.notification_messages
                    WHERE user_id = %s AND category = %s AND scheduled_on = %s
                    """,
                    (user_id, category, scheduled_on),
                )
                existing_message = await cur.fetchone()
                # Brand overflow must remain outside the event log so the
                # retention-window query can offer it again on a later day.
                if existing_message and category == BRAND_CATEGORY:
                    continue

                inserted: list[RecordedEvent] = []
                for event in category_events:
                    await cur.execute(
                        """
                        INSERT INTO ai.notifications (user_id, kind, product_id, brand_node_id, payload)
                        VALUES (%s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        (
                            event.user_id,
                            event.kind,
                            event.product_id,
                            event.brand_node_id,
                            json.dumps(event.payload, ensure_ascii=False, default=str),
                        ),
                    )
                    row = await cur.fetchone()
                    if row:
                        inserted.append(RecordedEvent(int(row[0]), event))
                if not inserted:
                    continue

                if existing_message:
                    existing_id, existing_status, existing_scheduled_at = existing_message
                    if existing_status == "pending" and existing_scheduled_at > now:
                        for item in inserted:
                            await cur.execute(
                                """
                                INSERT INTO ai.notification_message_events (message_id, notification_id)
                                VALUES (%s, %s)
                                """,
                                (existing_id, item.notification_id),
                            )
                        await cur.execute(
                            """
                            SELECT n.user_id, n.kind, n.product_id, n.brand_node_id, n.payload
                            FROM ai.notification_message_events me
                            JOIN ai.notifications n ON n.id = me.notification_id
                            WHERE me.message_id = %s
                            ORDER BY n.id
                            """,
                            (existing_id,),
                        )
                        merged_events = [
                            Event(
                                user_id=row[0],
                                kind=row[1],
                                product_id=int(row[2]),
                                brand_node_id=row[3],
                                payload=row[4],
                            )
                            for row in await cur.fetchall()
                        ]
                        digest = build_digest(merged_events)
                        payload = _message_payload(existing_id, category, merged_events)
                        await cur.execute(
                            """
                            UPDATE ai.notification_messages
                            SET title = %s, body = %s, payload = %s::jsonb
                            WHERE message_id = %s
                            """,
                            (digest.title, digest.body, json.dumps(payload, ensure_ascii=False), existing_id),
                        )
                        await cur.execute(
                            "UPDATE ai.notifications SET processed_at = now() WHERE id = ANY(%s)",
                            ([item.notification_id for item in inserted],),
                        )
                    else:
                        await cur.execute(
                            """
                            UPDATE ai.notifications
                            SET processed_at = now(), suppressed_reason = 'daily_cap'
                            WHERE id = ANY(%s)
                            """,
                            ([item.notification_id for item in inserted],),
                        )
                    recorded += len(inserted)
                    continue

                message_id = uuid4()
                digest = build_digest([item.event for item in inserted])
                payload = _message_payload(message_id, category, [item.event for item in inserted])
                await cur.execute(
                    """
                    INSERT INTO ai.notification_messages
                        (message_id, user_id, category, scheduled_on, scheduled_at,
                         expires_at, title, body, payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        message_id,
                        user_id,
                        category,
                        scheduled_on,
                        scheduled_at,
                        expires_at,
                        digest.title,
                        digest.body,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
                for item in inserted:
                    await cur.execute(
                        """
                        INSERT INTO ai.notification_message_events (message_id, notification_id)
                        VALUES (%s, %s)
                        """,
                        (message_id, item.notification_id),
                    )
                await cur.execute(
                    "UPDATE ai.notifications SET processed_at = now() WHERE id = ANY(%s)",
                    ([item.notification_id for item in inserted],),
                )
                await cur.execute(
                    """
                    INSERT INTO ai.notification_deliveries (message_id, device_id, next_attempt_at)
                    SELECT %s, d.device_id, %s
                    FROM ai.devices d
                    WHERE d.user_id = %s
                      AND d.provider = 'apns'
                      AND d.platform = 'ios'
                      AND d.status = 'active'
                    ON CONFLICT DO NOTHING
                    RETURNING delivery_id
                    """,
                    (message_id, scheduled_at, user_id),
                )
                delivery_rows = await cur.fetchall()
                if not delivery_rows:
                    await cur.execute(
                        """
                        UPDATE ai.notification_messages
                        SET status = 'no_recipient', completed_at = now()
                        WHERE message_id = %s
                        """,
                        (message_id,),
                    )
                recorded += len(inserted)
                messages += 1
                deliveries += len(delivery_rows)

    return len(baselines), recorded, messages, deliveries


async def claim_due_deliveries(
    pool: AsyncConnectionPool,
    *,
    now: datetime,
    limit: int,
) -> list[DeliveryTask]:
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE ai.notification_deliveries d
            SET status = 'expired', updated_at = now(), leased_until = NULL
            FROM ai.notification_messages m
            WHERE d.message_id = m.message_id
              AND d.status IN ('pending', 'retry', 'processing')
              AND m.expires_at <= %s
            """,
            (now,),
        )
        await cur.execute(
            """
            UPDATE ai.notification_messages m
            SET status = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM ai.notification_deliveries d
                        WHERE d.message_id = m.message_id AND d.status = 'accepted'
                    ) THEN 'partial'
                    ELSE 'expired'
                END,
                completed_at = COALESCE(completed_at, now())
            WHERE m.expires_at <= %s
              AND m.status IN ('pending', 'processing')
              AND NOT EXISTS (
                  SELECT 1 FROM ai.notification_deliveries d
                  WHERE d.message_id = m.message_id
                    AND d.status IN ('pending', 'retry', 'processing')
              )
            """,
            (now,),
        )
        await cur.execute(
            """
            WITH picked AS (
                SELECT d.delivery_id
                FROM ai.notification_deliveries d
                JOIN ai.notification_messages m ON m.message_id = d.message_id
                JOIN ai.devices dev ON dev.device_id = d.device_id
                WHERE (
                        (d.status IN ('pending', 'retry') AND d.next_attempt_at <= %s)
                        OR (d.status = 'processing' AND d.leased_until < %s)
                      )
                  AND m.scheduled_at <= %s
                  AND m.expires_at > %s
                  AND dev.status = 'active'
                ORDER BY d.next_attempt_at, d.delivery_id
                FOR UPDATE OF d SKIP LOCKED
                LIMIT %s
            ), claimed AS (
                UPDATE ai.notification_deliveries d
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    last_attempt_at = %s,
                    leased_until = %s + interval '5 minutes',
                    updated_at = now()
                FROM picked
                WHERE d.delivery_id = picked.delivery_id
                RETURNING d.delivery_id, d.message_id, d.device_id, d.apns_id, d.attempt_count
            )
            SELECT c.delivery_id, c.message_id, c.device_id,
                   dev.push_token, dev.environment, dev.registered_at,
                   m.category, m.scheduled_on, m.expires_at, m.title, m.body, m.payload,
                   c.apns_id, c.attempt_count
            FROM claimed c
            JOIN ai.devices dev ON dev.device_id = c.device_id
            JOIN ai.notification_messages m ON m.message_id = c.message_id
            """,
            (now, now, now, now, limit, now, now),
        )
        rows = await cur.fetchall()
    return [
        DeliveryTask(
            delivery_id=row[0],
            message_id=row[1],
            device_id=row[2],
            push_token=row[3],
            environment=row[4],
            registered_at=row[5],
            category=row[6],
            scheduled_on=row[7],
            expires_at=row[8],
            title=row[9],
            body=row[10],
            payload=row[11],
            apns_id=row[12],
            attempt_count=row[13],
        )
        for row in rows
    ]


def _retry_delay(result: apns.ApnsResult, attempt: int) -> timedelta:
    if result.retry_after_s is not None:
        seconds = result.retry_after_s
    elif result.configuration_error:
        seconds = 3600
    else:
        seconds = min(7200, 30 * (4 ** max(0, attempt - 1)))
    return timedelta(seconds=seconds + random.uniform(0, min(30, seconds * 0.1)))


async def _refresh_message_status(cur: Any, message_id: UUID) -> None:
    await cur.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'accepted'),
               count(*) FILTER (WHERE status IN ('pending', 'retry', 'processing')),
               count(*) FILTER (WHERE status IN ('failed', 'expired'))
        FROM ai.notification_deliveries
        WHERE message_id = %s
        """,
        (message_id,),
    )
    accepted, pending, failed = await cur.fetchone()
    if pending:
        message_status = "processing"
        completed_at = None
    elif accepted and failed:
        message_status = "partial"
        completed_at = datetime.now(tz=UTC)
    elif accepted:
        message_status = "accepted"
        completed_at = datetime.now(tz=UTC)
    else:
        message_status = "failed"
        completed_at = datetime.now(tz=UTC)
    await cur.execute(
        """
        UPDATE ai.notification_messages
        SET status = %s, completed_at = %s
        WHERE message_id = %s
        """,
        (message_status, completed_at, message_id),
    )
    if accepted:
        await cur.execute(
            """
            UPDATE ai.notifications n
            SET sent_at = COALESCE(n.sent_at, now())
            FROM ai.notification_message_events me
            WHERE me.message_id = %s AND me.notification_id = n.id
            """,
            (message_id,),
        )


async def _finish_delivery(
    pool: AsyncConnectionPool,
    task: DeliveryTask,
    result: apns.ApnsResult,
    *,
    now: datetime,
) -> str:
    max_attempts = max(1, settings.NOTIFY_MAX_DELIVERY_ATTEMPTS)
    retry_at = now + _retry_delay(result, task.attempt_count)
    can_retry = (result.retryable or result.configuration_error) and task.attempt_count < max_attempts
    can_retry = can_retry and retry_at < task.expires_at

    if result.ok:
        outcome = "accepted"
    elif can_retry:
        outcome = "retry"
    elif now >= task.expires_at:
        outcome = "expired"
    else:
        outcome = "failed"

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE ai.notification_deliveries
            SET status = %s,
                next_attempt_at = CASE WHEN %s = 'retry' THEN %s ELSE next_attempt_at END,
                leased_until = NULL,
                last_http_status = %s,
                last_reason = %s,
                accepted_at = CASE WHEN %s = 'accepted' THEN now() ELSE accepted_at END,
                updated_at = now()
            WHERE delivery_id = %s
            """,
            (outcome, outcome, retry_at, result.status, result.reason, outcome, task.delivery_id),
        )
        if result.ok:
            await cur.execute(
                """
                UPDATE ai.devices
                SET failure_count = 0, last_error = NULL
                WHERE device_id = %s
                """,
                (task.device_id,),
            )
        elif result.token_is_dead:
            invalidated_at = result.invalidated_at or now
            await cur.execute(
                """
                UPDATE ai.devices
                SET status = 'inactive', invalidated_at = %s, last_error = %s
                WHERE device_id = %s AND registered_at <= %s
                """,
                (invalidated_at, result.reason, task.device_id, invalidated_at),
            )
        elif result.reason == "BadDeviceToken":
            await cur.execute(
                """
                UPDATE ai.devices
                SET failure_count = failure_count + 1,
                    last_error = %s,
                    status = CASE WHEN failure_count + 1 >= 3 THEN 'quarantined' ELSE status END,
                    disabled_at = CASE WHEN failure_count + 1 >= 3 THEN now() ELSE disabled_at END
                WHERE device_id = %s
                """,
                (result.reason, task.device_id),
            )
        await _refresh_message_status(cur, task.message_id)
    return outcome


async def deliver_pending(
    pool: AsyncConnectionPool,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> DeliveryReport:
    now = now or datetime.now(tz=UTC)
    tasks = await claim_due_deliveries(
        pool,
        now=now,
        limit=limit or max(1, settings.NOTIFY_DELIVERY_BATCH_SIZE),
    )
    report = DeliveryReport(claimed=len(tasks))
    if not tasks:
        return report

    clients = {
        environment: apns.ApnsClient(environment=environment) for environment in {task.environment for task in tasks}
    }
    semaphore = asyncio.Semaphore(max(1, settings.NOTIFY_APNS_CONCURRENCY))

    async def send_one(task: DeliveryTask) -> str:
        async with semaphore:
            priority = 10 if task.category == SAVED_CATEGORY else 5
            try:
                result = await clients[task.environment].send(
                    task.push_token,
                    title=task.title,
                    body=task.body,
                    data=task.payload,
                    collapse_id=f"kiko-{task.category}-{task.scheduled_on.isoformat()}",
                    apns_id=str(task.apns_id),
                    priority=priority,
                    expiration=task.expires_at,
                )
            except Exception as exc:  # isolate one endpoint from the claimed batch
                logger.exception(
                    "🔔 [notify.worker] provider client failed delivery=%s error=%s",
                    task.delivery_id,
                    type(exc).__name__,
                )
                result = apns.ApnsResult(
                    device_token=task.push_token,
                    status=0,
                    reason="ProviderClientError",
                    apns_id=str(task.apns_id),
                )
            return await _finish_delivery(pool, task, result, now=now)

    try:
        outcomes = await asyncio.gather(*(send_one(task) for task in tasks))
    finally:
        await asyncio.gather(*(client.aclose() for client in clients.values()))

    report.accepted = outcomes.count("accepted")
    report.retried = outcomes.count("retry")
    report.failed = outcomes.count("failed")
    report.expired = outcomes.count("expired")
    return report


# ── Detection orchestration ─────────────────────────────────────────────────


async def run_notify_batch(
    pool: AsyncConnectionPool,
    *,
    dry_run: bool = False,
    only_user: UUID | None = None,
    limit: int | None = None,
    include_saved: bool = True,
    include_brand: bool = True,
    now: datetime | None = None,
) -> BatchReport:
    """Detect events and atomically enqueue durable per-device deliveries."""
    report = BatchReport(dry_run=dry_run)
    cutoff = now or datetime.now(tz=UTC)

    save_events: list[Event] = []
    baselines: list[BaselineWrite] = []
    if include_saved:
        saved_rows = await fetch_saved_rows(pool, only_user=only_user)
        save_events, baselines = detect_save_events(saved_rows, threshold=settings.NOTIFY_PRICE_DROP_THRESHOLD)

    brand_events: list[Event] = []
    if include_brand:
        since = cutoff - timedelta(days=settings.NOTIFY_NEW_PRODUCT_WINDOW_D)
        new_rows = await fetch_new_product_rows(pool, since=since, cutoff=cutoff, only_user=only_user)
        brand_events = new_product_events(new_rows)

    events = [*save_events, *brand_events]
    for event in events:
        report.detected[event.kind] += 1

    user_ids = sorted({event.user_id for event in events}, key=str)
    prefs = await fetch_preferences(pool, user_ids)
    brand_days = await fetch_brand_new_days_last_week(pool, user_ids)
    selected = select_for_delivery(
        events,
        prefs=prefs,
        brand_new_days_last_week=brand_days,
        weekly_cap=settings.NOTIFY_BRAND_NEW_WEEKLY_CAP,
        max_brand_items=settings.NOTIFY_BRAND_NEW_MAX_ITEMS,
    )
    if limit is not None:
        allowed_users = set(list(selected)[: max(0, limit)])
        selected = {user_id: value for user_id, value in selected.items() if user_id in allowed_users}
        baselines = [baseline for baseline in baselines if baseline.user_id in allowed_users]
    for user_events in selected.values():
        for event in user_events:
            report.selected[event.kind] += 1

    if dry_run:
        logger.info(report.as_line())
        return report

    (
        report.baselines_written,
        report.recorded,
        report.messages_queued,
        report.deliveries_queued,
    ) = await _persist_outbox(pool, selected=selected, baselines=baselines, now=cutoff)
    if include_saved and not include_brand:
        job_name = SAVED_JOB
    elif include_brand and not include_saved:
        job_name = BRAND_JOB
    else:
        job_name = NOTIFY_JOB
    await record_run(pool, job_name, cutoff)
    logger.info(report.as_line())
    return report
