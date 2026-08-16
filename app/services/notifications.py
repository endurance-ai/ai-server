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
KIND_BRAND_SALE = "brand_sale"
KINDS: tuple[str, ...] = (KIND_RESTOCK, KIND_PRICE_DROP, KIND_BRAND_NEW, KIND_BRAND_SALE)

# ai.brand_news.kind 값. 알림 kind 와 이름이 겹치지만 다른 축이다 — 이쪽은 "브랜드
# 단위 소식의 종류" 고, KIND_* 는 "유저에게 가는 알림의 종류" 다. NEWS_BRAND_SALE 만
# 알림함과 브랜드 홈이 공유하고, NEWS_BRAND_NEW 는 브랜드 홈 전용이다 (알림함은
# 상품별 brand_new_product 행을 따로 갖고 있어 끼워 넣으면 중복이다).
NEWS_BRAND_SALE = "brand_sale"
NEWS_BRAND_NEW = "brand_new"

# 마지막 실행 시각을 남기는 ai.notification_job_state 행 키 (관측용 — 감지는 이 값을
# 읽지 않는다. 이유는 _NEW_PRODUCT_SQL 주석 참조).
NOTIFY_JOB = "notify_batch"
SAVED_JOB = "notify_saved_detect"
BRAND_JOB = "notify_brand_detect"
SAVED_CATEGORY = "saved_product_digest"
BRAND_CATEGORY = "brand_new_digest"
# brand_sale 전용 다이제스트 카테고리 — brand_new(BRAND_CATEGORY)와 분리한다.
# 공유하면 fetch_brand_new_days_last_week 의 주간 캡 카운트에 세일 발송이 섞인다.
BRAND_SALE_CATEGORY = "brand_sale_digest"

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
class BrandSaleRow:
    """브랜드별 세일 비율 집계 + 직전 세일 상태 한 행.

    detect_save_events 의 SavedRow 처럼 판정에 필요한 값을 이미 조인해 담는다.
    팔로워는 여기 없다 — 소식은 브랜드 단위 사실이고 팬아웃은 그 다음 단계다.
    (0027 이전엔 followers 가 이 행에 붙어 있었고, 그래서 팔로워 0명 브랜드는
    감지 자체가 되지 않았다. fetch_brand_followers 로 분리했다.)
    """

    brand_node_id: int
    brand: str | None
    sale_count: int
    total_count: int
    prev_on_sale: bool
    # 이 브랜드 현재 세일 중 가장 깊은 개별 상품 할인율 (0~1). 발동 임계값은
    # sale_count/total_count 비율이지만, 문구의 "최대 N% 싸요" 는 이 값(=가장 큰
    # 단일 상품 할인)을 쓴다 — 카탈로그 커버리지 비율이 아니라 실제 최대 할인폭.
    max_discount_pct: float | None = None


@dataclass(frozen=True, slots=True)
class Event:
    user_id: UUID
    kind: str
    product_id: int | None
    brand_node_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BrandSaleStateWrite:
    brand_node_id: int
    on_sale: bool
    ratio: float


@dataclass(frozen=True, slots=True)
class BrandNewsWrite:
    """ai.brand_news 정본 행의 열기/닫기 지시 (migration 0027).

    opened=True  → 세일 시작. 새 소식 행을 연다 (started_at=now, ended_at=NULL).
    opened=False → 세일 종료. 진행 중이던 행을 닫는다 (ended_at=now).

    상태(ai.brand_sale_state)가 "지금 세일 중인가" 라는 현재값이라면 이쪽은 이력이다.
    둘 다 같은 전환에서 파생되지만 상태는 덮어쓰고 소식은 쌓인다.
    """

    brand_node_id: int
    kind: str
    payload: dict[str, Any]
    opened: bool


@dataclass(frozen=True, slots=True)
class BrandSalePersistResult:
    """_persist_brand_sale 결과. 위치 인자 5개 튜플이 한계에 다다라 이름을 붙였다.

    suppressed_* 는 0028 이전에 ai.notifications.suppressed_reason 컬럼으로 남기던
    관측값이다. 이제 막힌 유저는 행 자체를 만들지 않으므로(인박스는 정본이 담당)
    리포트 카운터로만 센다.
    """

    news_written: int = 0
    recorded: int = 0
    messages: int = 0
    deliveries: int = 0
    pushed: int = 0
    suppressed_consent: int = 0
    suppressed_cap: int = 0


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
    # ai.brand_news 에 열거나 닫은 행 수. 두 종류는 트리거도 소비자도 달라 따로 센다
    # (세일=전환 감지·알림함+브랜드홈 / 신상=롤링 집계·브랜드홈 전용).
    brand_sale_news: int = 0
    brand_new_news: int = 0
    recorded: int = 0
    messages_queued: int = 0
    deliveries_queued: int = 0
    users_pushed: int = 0
    # 푸시가 게이트에 막힌 이벤트 수. 어느 쪽도 "유저가 못 본 소식" 이 아니다 —
    # brand_sale 은 정본(ai.brand_news)이, 찜 알림은 인박스 전용 행이 노출을 담당한다.
    push_suppressed_consent: int = 0
    push_suppressed_cap: int = 0
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
            f"baselines={self.baselines_written} "
            f"brand_news(sale={self.brand_sale_news} new={self.brand_new_news}) "
            f"recorded={self.recorded} "
            f"messages={self.messages_queued} deliveries={self.deliveries_queued} "
            f"users_pushed={self.users_pushed} ok={self.pushes_ok} failed={self.pushes_failed} "
            f"suppressed(consent={self.push_suppressed_consent} cap={self.push_suppressed_cap}) "
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


def detect_brand_sale_events(
    rows: list[BrandSaleRow],
    *,
    followers: dict[int, tuple[UUID, ...]],
    threshold: float = 0.30,
) -> tuple[list[Event], list[BrandSaleStateWrite], list[BrandNewsWrite]]:
    """브랜드별 세일 비율이 임계값을 **처음 넘는 순간에만** 소식을 만든다.

    재입고 판정(baseline_in_stock false→true, detect_save_events)과 같은 전환 패턴이다.
    비율이 임계값 이상이면 on_sale=true 를 항상 기록해 두고, 직전 상태가 false 였을
    때만 소식을 연다 — 연속 세일 기간 내내 매 배치 재발송하지 않는다.

    **소식(BrandNewsWrite)이 먼저이고 유저 이벤트는 그 팬아웃이다.** 팔로워가 하나도
    없어도 소식은 만들어진다 — 브랜드 홈은 비로그인도 보는 페이지라 노출할 컨텐츠가
    팔로우 여부에 종속되면 안 된다. followers 에 없는 브랜드는 events 만 비고
    news/state 는 그대로 나간다.

    state 는 이벤트 발생 여부와 무관하게 항상 현재 비율/상태로 갱신한다. 그래야
    다음 배치가 정확한 직전 상태를 보고 false→true 전환을 판정할 수 있다.
    """
    events: list[Event] = []
    states: list[BrandSaleStateWrite] = []
    news: list[BrandNewsWrite] = []

    for row in rows:
        ratio = row.sale_count / row.total_count if row.total_count > 0 else 0.0
        now_on_sale = ratio >= threshold
        states.append(BrandSaleStateWrite(row.brand_node_id, now_on_sale, ratio))

        if row.prev_on_sale and not now_on_sale:
            # 세일 종료 — 진행 중이던 소식을 닫는다. payload 는 닫기에 쓰이지 않는다.
            news.append(BrandNewsWrite(row.brand_node_id, NEWS_BRAND_SALE, {}, opened=False))
            continue

        if not (now_on_sale and not row.prev_on_sale):
            continue

        # ratio/sale_pct 는 발동 신호(카탈로그 세일 커버리지)라 관측/디버깅용으로
        # 남긴다. 문구가 실제로 쓰는 값은 max_discount_pct(가장 깊은 단일 할인)다.
        payload = {
            "brand": row.brand,
            "ratio": round(ratio, 3),
            "sale_pct": round(ratio * 100),
            "max_discount_pct": round(row.max_discount_pct * 100) if row.max_discount_pct else None,
        }
        news.append(BrandNewsWrite(row.brand_node_id, NEWS_BRAND_SALE, payload, opened=True))

        for user_id in followers.get(row.brand_node_id, ()):
            events.append(
                Event(
                    user_id=user_id,
                    kind=KIND_BRAND_SALE,
                    product_id=None,  # 브랜드 단위 알림 — 특정 상품에 매이지 않는다.
                    brand_node_id=row.brand_node_id,
                    payload=payload,
                )
            )

    return events, states, news


def kind_allowed(prefs: dict[str, Any] | None, kind: str) -> bool:
    """Apply the existing mobile master/marketing controls plus kind opt-out."""
    values = prefs or {}
    if not bool(values.get("system", True)):
        return False
    # brand_sale 은 전용 동의 키가 설정 스키마에 없어 브랜드 소식(brand_new_product)
    # 동의를 공유한다 — 프론트가 두 종류를 한 "브랜드 알림" 토글로 묶는다.
    consent_kind = KIND_BRAND_NEW if kind == KIND_BRAND_SALE else kind
    if consent_kind == KIND_BRAND_NEW and not bool(values.get("release_alerts", True)):
        return False
    return bool(values.get(consent_kind, DEFAULT_KIND_CONSENT))


def select_for_delivery(
    events: list[Event],
    *,
    prefs: dict[UUID, dict[str, Any] | None],
    brand_new_days_last_week: dict[UUID, int],
    weekly_cap: int,
    max_brand_items: int,
    max_per_brand: int,
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
        brand_new = pick_brand_new(
            [e for e in user_events if e.kind == KIND_BRAND_NEW],
            max_items=max_brand_items,
            max_per_brand=max_per_brand,
        )
        # brand_sale 은 이 경로를 타지 않는다 — 인박스 무조건 적재 + 푸시 게이트가
        # 서로 달라 _persist_brand_sale 이 전담한다(인박스는 동의/캡과 무관하게 항상,
        # 푸시만 kind_allowed + 주간 캡으로 게이트).
        selected = [
            *[e for e in user_events if e.kind == KIND_RESTOCK],
            *[e for e in user_events if e.kind == KIND_PRICE_DROP],
            *brand_new,
        ]
        if selected:
            ordered[user_id] = selected
    return ordered


def pick_brand_new(events: list[Event], *, max_items: int, max_per_brand: int) -> list[Event]:
    """유저 한 명분 brand_new 후보에서 하루치를 고른다. 입력은 최신순 전제.

    단순히 앞에서 max_items 개를 자르면 **한 브랜드가 하루 몫을 독식**한다.
    products.created_at 은 출시일이 아니라 우리 DB 적재 시각이고, 크롤은 브랜드 단위로
    통째 돌아서 한 브랜드의 수백 건이 같은 초에 들어온다(실측: 한 브랜드 775건이 2초).
    그래서 "가장 새로운 5개" 가 사실상 "마지막에 크롤된 브랜드 5개" 가 되고, 나머지
    팔로우 브랜드는 영영 묻힌다.

    브랜드당 상한을 먼저 적용해 고르게 뽑고, 슬롯이 남으면 상한 초과분으로 채운다.
    채우기 단계가 없으면 브랜드를 하나만 팔로우한 유저는 하루 max_per_brand 개로
    줄어든다 — 다양성 장치가 물량 축소로 변질되면 안 된다.

    출력은 다시 입력 순서(최신순)로 맞춘다. build_digest 가 events[0] 로 대표 상품을
    고르므로 두 단계로 뽑았다는 사실이 문구에 새어나가면 안 된다.
    """
    picked: list[tuple[int, Event]] = []
    overflow: list[tuple[int, Event]] = []
    per_brand: dict[int | None, int] = {}

    for index, event in enumerate(events):
        if per_brand.get(event.brand_node_id, 0) < max_per_brand:
            per_brand[event.brand_node_id] = per_brand.get(event.brand_node_id, 0) + 1
            picked.append((index, event))
        else:
            overflow.append((index, event))

    picked = picked[:max_items]
    for item in overflow:
        if len(picked) >= max_items:
            break
        picked.append(item)

    return [event for _, event in sorted(picked, key=lambda pair: pair[0])]


def consent_suppressed_events(
    events: list[Event],
    *,
    prefs: dict[UUID, dict[str, Any] | None],
    max_brand_items: int,
    max_per_brand: int,
) -> list[Event]:
    """동의 게이트에 막혔지만 인박스에는 남길 이벤트. 인박스 적재만, 푸시는 없다.

    푸시를 끈 것과 소식을 못 보는 것은 다른 일이다. 그런데 찜 알림은 그 둘이 묶여
    있어서, 동의가 꺼져 있으면 select_for_delivery 에서 탈락하고 _persist_outbox 가
    행을 만들지 않아 **알림함에서도** 사라졌다.

    찜 알림은 이 손실이 되돌릴 수 없다는 점에서 더 나빴다. detect_save_events 가
    발동과 함께 기준가/기준재고를 전진시키고, run_notify_batch 는 그 기준값을 선별
    여부와 무관하게 저장한다. 즉 이벤트는 이미 소비돼 다시는 감지되지 않는다 —
    나중에 동의를 켜도 그 하락은 영영 볼 수 없다.

    brand_sale 처럼 정본을 조회하는 방식(read fan-out)으로는 풀 수 없다. "네가 찜한
    셔츠가 재입고됐다" 는 유저마다 다른 사실이라 접을 정본이 없다. 그래서 대신
    **인박스 행은 남기고 푸시만 게이트하는** 방식을 쓴다.

    **동의 실패만 여기 들어온다.** 주간 캡·하루 상한(overflow)에 막힌 brand_new 는
    제외한다 — _NEW_PRODUCT_SQL 이 ai.notifications 안티조인으로 후보를 고르므로,
    기록해버리면 다음 날 다시 후보가 되는 재시도가 깨진다. 동의 off 는 영구 억제라
    재시도할 게 없어서 기록해도 안전하다.

    brand_new 는 인박스 적재도 푸시 경로와 **같은 선별**(pick_brand_new)을 거친다.
    안 그러면 동의를 꺼둔 유저에게 14일 보존창의 후보 수백 건이 한 배치에 쏟아지고,
    브랜드 쏠림도 그대로 인박스에 재현된다. 잘린 몫은 기록되지 않아 다음 날 다시
    후보가 되므로 푸시 경로와 같은 속도로 흘러든다.
    """
    suppressed: list[Event] = []
    brand_new_by_user: dict[UUID, list[Event]] = {}

    for event in events:
        if event.kind not in (KIND_RESTOCK, KIND_PRICE_DROP, KIND_BRAND_NEW):
            continue
        if kind_allowed(prefs.get(event.user_id), event.kind):
            continue
        if event.kind == KIND_BRAND_NEW:
            brand_new_by_user.setdefault(event.user_id, []).append(event)
        else:
            suppressed.append(event)

    for user_events in brand_new_by_user.values():
        suppressed.extend(pick_brand_new(user_events, max_items=max_brand_items, max_per_brand=max_per_brand))
    return suppressed


def build_digest(events: list[Event], *, notification_ids: list[int] | None = None) -> Digest:
    """유저 한 명분 알림을 푸시 한 건으로 묶는다."""
    if not events:
        raise ValueError("build_digest requires at least one event")

    kinds = {e.kind for e in events}
    if kinds == {KIND_BRAND_NEW}:
        brands = {e.payload.get("brand") for e in events if e.payload.get("brand")}
        brand_label = next(iter(brands)) if len(brands) == 1 else "관심 브랜드"
        title, body = f"{brand_label}에 신상 {len(events)}개가 들어왔어요", ""
    elif kinds == {KIND_BRAND_SALE}:
        # 한 브랜드면 브랜드명으로, 여러 브랜드가 동시에 전환됐으면 곳 수로 묶는다.
        # (이 분기가 len==1 폴백보다 먼저 걸려 brand_sale 은 _single_copy 를 타지 않는다.)
        if len(events) == 1:
            title, body = _single_copy(events[0])
        else:
            title, body = f"팔로우한 브랜드 {len(events)}곳이 세일 중이에요", ""
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
    """찜 알림(가격 하락 · 재입고) + 브랜드 세일 한 건짜리 문구. PRD 알림 문안 표 참조."""
    if event.kind == KIND_BRAND_SALE:
        brand = event.payload.get("brand")
        # PRD 문안: 제목 "팔로우한 {브랜드명} 세일 시작했어요" / 본문 "최대 N% 싸요".
        # N 은 카탈로그 커버리지 비율이 아니라 가장 깊은 개별 상품 할인율이다.
        title = f"팔로우한 {brand} 세일 시작했어요" if brand else "팔로우한 브랜드 세일 시작했어요"
        max_discount_pct = event.payload.get("max_discount_pct")
        body = f"최대 {max_discount_pct}% 싸요" if max_discount_pct else ""
        return title, body
    if event.kind == KIND_PRICE_DROP:
        brand = event.payload.get("brand")
        title = f"찜하신 {brand} 상품이 할인되었어요" if brand else "찜한 상품이 할인되었어요"
        body = _price_range(event.payload.get("baseline_price"), event.payload.get("price"))
        return title, body
    name = event.payload.get("name") or "상품"
    return f"찜한 {name}이 재입고되었어요", ""


def brand_news_copy(kind: str, payload: dict[str, Any]) -> tuple[str, str]:
    """브랜드 홈 소식 카드 문구 (text, sub). ai.brand_news.payload 만 보고 만든다.

    인박스(_single_copy / api.notifications._copy)와 문안이 다른 이유: 그쪽은 피드에서
    맥락 없이 읽히므로 "팔로우한 {브랜드}" 로 대상을 밝혀야 하지만, 브랜드 홈은 이미
    그 브랜드 페이지 안이라 브랜드명 반복이 군더더기다. 팔로우하지 않은 유저·비로그인도
    보는 화면이라 "팔로우한" 이라는 전제 자체가 틀리기도 한다.
    """
    if kind == NEWS_BRAND_SALE:
        max_discount_pct = payload.get("max_discount_pct")
        sub = f"최대 {max_discount_pct}% 싸요" if max_discount_pct else ""
        return "세일 시작했어요", sub
    if kind == NEWS_BRAND_NEW:
        # 기간을 문구에 박지 않는다 — 집계 창이 설정값
        # (NOTIFY_BRAND_NEW_SUMMARY_WINDOW_D)이라 "이번 주" 가 거짓말이 될 수 있다.
        new_count = payload.get("new_count")
        return (f"신상 {new_count}개가 새로 들어왔어요", "") if new_count else ("새 상품이 들어왔어요", "")
    return "새 소식이 있어요", ""


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
    # 방어용 — brand_sale 은 전용 카테고리라 이 혼합-종류 폴백 경로에 도달하지 않는다
    # (_persist_outbox 가 category 별로 다이제스트를 나눠 brand_sale 이 단독으로 묶인다).
    if event.kind == KIND_BRAND_SALE:
        brand = event.payload.get("brand") or "팔로우한 브랜드"
        return f"{brand} 세일 중이에요"
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

# 성별 정합은 큐레이션과 같은 술어를 그대로 쓴다 — 푸시는 검색보다 큐레이션에 가깝다
# (fail-closed: 성별을 확정할 수 없는 상품/유저는 조용히 제외). 큐레이션은 gender 를
# 배치 파라미터로 받지만 여긴 유저마다 달라 컬럼 참조로 바꾼다.
_GENDER_MATCH_PER_USER = GENDER_MATCH_SQL.replace("%(gender)s", "k.gender_app")

# 워터마크가 아니라 **보존창 + 안티조인**이다.
#
# 도입 당시 근거는 "크롤러가 gender 를 안 쓰므로 신상은 항상 성별 미해결 상태를
# 거친다" 였는데, **그 전제는 이제 틀렸다** — gender 소유권이 크롤러로 돌아왔고
# `chk_products_gender_required` 가 VALIDATE 되어 products.gender 는 NULL 일 수
# 없다. 상품 쪽 "나중에 해결" 케이스는 사라졌다.
#
# 그래도 보존창을 유지하는 이유는 남아 있다:
#   ① **유저 쪽** 미해결 — user_profiles.gender 가 비어 있으면 picks 에서 빠지고,
#      나중에 채워지면 창이 살아 있는 동안 다시 후보가 된다.
#   ② 캡/상한에 걸려 못 나간 상품도 다음 회차에 다시 후보가 된다.
# 워터마크였다면 둘 다 영구 유실이다.
# 안티조인은 날짜와 무관하다 — brand_new_product 는 (user, product) 당 평생 1회다.
# 온보딩 적재분 제외 (실측 근거는 _BRAND_FIRST_SEEN_CTE 주석 참조). 브랜드를 처음
# 수집하면 카탈로그 전체가 같은 배치로 들어와 전부 "신상" 으로 잡히므로, 브랜드 최초
# 적재 후 유예시간 안에 들어온 행은 후보에서 뺀다.
_BRAND_FIRST_SEEN_CTE = """
    brand_first_seen AS (
        SELECT brand_node_id, min(created_at) AS first_seen_at
        FROM public.products
        WHERE brand_node_id IS NOT NULL
        GROUP BY brand_node_id
    )
"""
_NOT_ONBOARDING_IMPORT = """
    p.created_at > f.first_seen_at + make_interval(hours => %(onboarding_grace_h)s)
"""

# 조인 순서를 **팔로우 브랜드 → 상품 → 유저** 로 고정한다.
#
# 술어를 한 평면에 늘어놓으면 플래너가 `user_profiles × products`(성별 GIN 매칭)를 먼저
# 전개하고 팔로우 브랜드로 나중에 걸렀다. 즉 중간 결과가 **유저 수 × 성별매칭 카탈로그**로
# 커진다 — 실측(유저 84명, 상품 212k): 중간 2,418,360행 / 1.8초, 플래너 추정은 368행으로
# 실제 12,441행 대비 34배 과소추정. 유저 1만이면 같은 형태로 ~288M 행이라 배치 창을 넘긴다.
#
# 유저는 늘어도 **팔로우된 브랜드 수와 최근 창의 상품 수는 카탈로그에 묶여 있다**. 그래서
# 후보 상품(`candidates`)을 유저와 무관하게 한 번만 만들고, 그 다음에 picks 를 붙인다.
# `MATERIALIZED` 는 플래너가 CTE 를 다시 인라인해 원래의 나쁜 순서로 되돌리는 것을 막는다.
# `candidates` 는 `idx_products_notification_brand_created (brand_node_id, created_at DESC,
# id DESC) WHERE brand_node_id IS NOT NULL` 을 타라고 만든 형태다.
#
# brand_first_seen 도 팔로우 브랜드로 좁힌다. min(created_at) 은 브랜드별 집계라 대상
# 브랜드를 줄여도 값이 변하지 않는다 (전체 브랜드 집계는 브랜드 홈 요약 쪽
# _BRAND_NEW_SUMMARY_SQL 이 따로 쓴다).
#
# 술어·감지 의미는 하나도 바꾸지 않았다 — 보존창, 온보딩 유예, 성별 fail-closed 매칭,
# ai.notifications 안티조인, 정렬까지 그대로다.
#
# ── 하루 상한을 SQL 로 내린다 (`bucketed`) ──────────────────────────────────────
#
# 예전엔 후보 **전량**을 파이썬으로 올린 뒤 pick_brand_new 가 5건으로 잘랐다. 실측 유저당
# 최대 1,294건이라 260배를 실어 나르고 버리는 구조였고, 세 자료구조(NewProductRow → Event
# → per-user dict)가 동시에 상주해 후보당 약 780 B 를 썼다. 워커 컨테이너 상한 256 MB 를
# 후보 ~330,000건(유저 약 2,500명)에서 넘겨 OOM 으로 배치가 완료되지 않았다.
#
# `bucketed` 는 유저를 두 버킷으로 쪼갠다 — 브랜드 상한 이내(brand_rank <= max_per_brand)와
# 초과분 — 그리고 각 버킷에서 최신 max_items 개만 남긴다. **이 축소는 pick_brand_new 의
# 결과를 바꾸지 않는다**:
#
#   pick_brand_new 는 (1) 입력 순서대로 훑어 브랜드 상한 이내면 picked, 아니면 overflow,
#   (2) picked[:max_items], (3) 남은 슬롯을 overflow 앞에서부터 채움 — 이 순서다.
#   (2)가 쓰는 건 picked 앞 max_items 개뿐이고, (3)이 쓰는 건 overflow 앞 max_items 개를
#   넘지 않는다(빈 슬롯이 최대 max_items). 두 버킷의 앞부분을 그대로 보존했으므로
#   축소본에서도 브랜드 순위가 재계산되지 않는다 — 상한 이내 행의 접두사를 통째로 남기기
#   때문에 초과분이 상한 이내로 승격될 수 없다.
#
# 그래서 유저당 최대 2×max_items(기본 10)행만 올라온다. 등가성은
# test_sql_side_cap_matches_pick_brand_new 가 순수 함수로 고정한다.
#
# **관측 주의**: `report.detected[brand_new_product]` 가 이제 "후보 총량" 이 아니라
# "상한 적용 후 전달된 후보 수" 다. 예전 로그와 이 숫자를 직접 비교하면 안 된다.
_NEW_PRODUCT_SQL = f"""
    WITH picks AS MATERIALIZED (
        SELECT ubp.user_id,
               ubp.brand_id,
               CASE up.gender WHEN 'female' THEN 'women' WHEN 'male' THEN 'men' END AS gender_app
        FROM ai.user_brand_picks ubp
        JOIN ai.user_profiles up ON up.user_id = ubp.user_id
        WHERE up.gender IN ('female', 'male')
          AND (%(only_user)s::uuid IS NULL OR ubp.user_id = %(only_user)s::uuid)
    ),
    followed_brands AS MATERIALIZED (
        SELECT DISTINCT brand_id FROM picks
    ),
    brand_first_seen AS (
        SELECT brand_node_id, min(created_at) AS first_seen_at
        FROM public.products
        WHERE brand_node_id IN (SELECT brand_id FROM followed_brands)
        GROUP BY brand_node_id
    ),
    candidates AS MATERIALIZED (
        SELECT p.id, p.brand_node_id, p.brand, p.name, p.price, p.gender, p.created_at
        FROM followed_brands fb
        JOIN public.products p ON p.brand_node_id = fb.brand_id
        JOIN brand_first_seen f ON f.brand_node_id = p.brand_node_id
        {PRODUCT_FEATURES_JOIN}
        WHERE p.created_at > %(since)s
          AND p.created_at <= %(cutoff)s
          AND {_NOT_ONBOARDING_IMPORT}
          AND p.in_stock
          AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
    ),
    matched AS MATERIALIZED (
        SELECT k.user_id, p.id, p.brand_node_id, p.brand, p.name, p.price, p.created_at,
               row_number() OVER (
                   PARTITION BY k.user_id, p.brand_node_id
                   ORDER BY p.created_at DESC, p.id DESC
               ) AS brand_rank
        FROM candidates p
        JOIN picks k ON k.brand_id = p.brand_node_id
        WHERE {_GENDER_MATCH_PER_USER}
          AND NOT EXISTS (
              SELECT 1 FROM ai.notifications n
              WHERE n.user_id = k.user_id
                AND n.kind = '{KIND_BRAND_NEW}'
                AND n.product_id = p.id
          )
    ),
    bucketed AS (
        SELECT user_id, id, brand_node_id, brand, name, price, created_at,
               row_number() OVER (
                   PARTITION BY user_id, (brand_rank <= %(max_per_brand)s)
                   ORDER BY created_at DESC, id DESC
               ) AS bucket_rank
        FROM matched
    )
    SELECT user_id, id, brand_node_id, brand, name, price
    FROM bucketed
    WHERE bucket_rank <= %(max_items)s
    ORDER BY created_at DESC, id DESC
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
    onboarding_grace_h: int,
    only_user: UUID | None = None,
    max_items: int | None = None,
    max_per_brand: int | None = None,
) -> list[NewProductRow]:
    """유저당 하루 상한에 필요한 만큼만 가져온다 (_NEW_PRODUCT_SQL `bucketed` 주석 참조).

    상한 기본값은 pick_brand_new 에 넘기는 값과 **같은 설정**이어야 한다 — 다르면 SQL 이
    잘라낸 몫을 파이썬이 다시 고르지 못한다.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _NEW_PRODUCT_SQL,
            {
                "since": since,
                "cutoff": cutoff,
                "only_user": only_user,
                "onboarding_grace_h": onboarding_grace_h,
                "max_items": settings.NOTIFY_BRAND_NEW_MAX_ITEMS if max_items is None else max_items,
                "max_per_brand": (settings.NOTIFY_BRAND_NEW_MAX_PER_BRAND if max_per_brand is None else max_per_brand),
            },
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


# 브랜드별 세일 비율 = (sale_price < original_price 인 상품 수) / (전체 상품 수).
# in_stock 필터 없음 (전체 카탈로그 기준 — 사용자 확정). 성별 매칭도 없다 — 브랜드
# 단위 세일 소식은 상품 추천이 아니라 브랜드 헤즈업이라 신상(brand_new)의 성별 게이트가
# 필요 없다.
#
# **팔로워 조인이 없다** (0027). 소식은 브랜드 단위 사실이므로 팔로워가 없는 브랜드도
# 집계해 ai.brand_news 에 남겨야 브랜드 홈이 채워진다. 팬아웃 대상은
# fetch_brand_followers 가 따로 가져와 detect_brand_sale_events 에서 합친다.
# 그 대신 전체 브랜드를 훑게 되므로 HAVING 으로 카탈로그가 너무 작은 브랜드를 뺀다
# (상품 2개 중 1개 할인 = 비율 50% 짜리 가짜 전면세일 방지).
_BRAND_SALE_SQL = """
    SELECT p.brand_node_id,
           max(p.brand) AS brand,
           count(*) FILTER (
               WHERE p.sale_price IS NOT NULL
                 AND p.original_price IS NOT NULL
                 AND p.sale_price < p.original_price
           ) AS sale_count,
           count(*) AS total_count,
           coalesce(bss.on_sale, false) AS prev_on_sale,
           -- 문구용: 이 브랜드 현재 세일 중 가장 깊은 개별 상품 할인율(0~1).
           -- FILTER 가 sale_price < original_price 를 보장하므로 분모는 항상 양수다.
           max((p.original_price - p.sale_price)::numeric / NULLIF(p.original_price, 0)) FILTER (
               WHERE p.sale_price IS NOT NULL
                 AND p.original_price IS NOT NULL
                 AND p.sale_price < p.original_price
           ) AS max_discount
    FROM public.products p
    LEFT JOIN ai.brand_sale_state bss ON bss.brand_node_id = p.brand_node_id
    WHERE p.brand_node_id IS NOT NULL
    GROUP BY p.brand_node_id, bss.on_sale
    HAVING count(*) >= %(min_products)s
"""

# notify_enabled 팔로워 팬아웃 대상. only_user 는 여기만 좁힌다 — 비율/전환 판정은
# 브랜드 전역이라 카나리 실행이 다른 팔로워의 전환을 앞당겨 소비할 수 있다
# (0027 이후로는 소식 자체가 brand_news 에 남으므로 유실이 아니라 푸시 누락에 그친다).
_BRAND_FOLLOWERS_SQL = """
    SELECT ubp.brand_id, array_agg(ubp.user_id) AS user_ids
    FROM ai.user_brand_picks ubp
    WHERE ubp.notify_enabled
      AND (%(only_user)s::uuid IS NULL OR ubp.user_id = %(only_user)s::uuid)
    GROUP BY ubp.brand_id
"""


async def fetch_brand_sale_rows(pool: AsyncConnectionPool, *, min_products: int = 1) -> list[BrandSaleRow]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_BRAND_SALE_SQL, {"min_products": min_products})
        rows = await cur.fetchall()
    return [
        BrandSaleRow(
            brand_node_id=int(r[0]),
            brand=r[1],
            sale_count=int(r[2]),
            total_count=int(r[3]),
            prev_on_sale=bool(r[4]),
            max_discount_pct=float(r[5]) if r[5] is not None else None,
        )
        for r in rows
    ]


# 브랜드 홈 "신상 N개" 요약용 집계. 알림용 _NEW_PRODUCT_SQL 과 달리 팔로우도 성별도
# 보지 않는다 — 브랜드 홈은 비로그인도 보는 화면이라 개인화할 대상이 없다.
# in_stock + 이미지 조건은 알림 쪽과 맞춘다 (품절이나 이미지 없는 상품을 "신상 입고" 로
# 세면 눌렀을 때 보여줄 게 없다).
_BRAND_NEW_SUMMARY_SQL = f"""
    WITH {_BRAND_FIRST_SEEN_CTE}
    SELECT p.brand_node_id, max(p.brand) AS brand, count(*) AS new_count
    FROM public.products p
    JOIN brand_first_seen f ON f.brand_node_id = p.brand_node_id
    WHERE p.created_at > %(since)s
      AND p.created_at <= %(cutoff)s
      AND {_NOT_ONBOARDING_IMPORT}
      AND p.in_stock
      AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
    GROUP BY p.brand_node_id
"""


async def fetch_brand_new_counts(
    pool: AsyncConnectionPool, *, since: datetime, cutoff: datetime, onboarding_grace_h: int
) -> dict[int, tuple[str | None, int]]:
    """brand_node_id → (브랜드명, 최근 신상 수). 브랜드 홈 요약 소식의 입력."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _BRAND_NEW_SUMMARY_SQL,
            {"since": since, "cutoff": cutoff, "onboarding_grace_h": onboarding_grace_h},
        )
        return {int(r[0]): (r[1], int(r[2])) for r in await cur.fetchall()}


async def fetch_brand_followers(
    pool: AsyncConnectionPool, *, only_user: UUID | None = None
) -> dict[int, tuple[UUID, ...]]:
    """brand_node_id → notify_enabled 팔로워. 소식 감지와 분리된 팬아웃 대상 조회."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_BRAND_FOLLOWERS_SQL, {"only_user": only_user})
        rows = await cur.fetchall()
    return {int(r[0]): tuple(r[1]) for r in rows}


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


async def fetch_brand_sale_days_last_week(pool: AsyncConnectionPool, user_ids: list[UUID]) -> dict[UUID, int]:
    """최근 7일 중 brand_sale 푸시가 실제로 나간 **날짜 수**를 유저별로 센다.

    fetch_brand_new_days_last_week 와 같은 집계이되 카테고리만 BRAND_SALE_CATEGORY 로
    바꾼다 — brand_new 캡과 독립적으로 세기 위해 전용 카테고리로 분리돼 있다. 이
    카운트는 Phase B 푸시 게이트에만 쓰인다(인박스 적재는 이 캡과 무관하게 항상 함).
    """
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
            (BRAND_SALE_CATEGORY, user_ids, settings.NOTIFY_TIMEZONE),
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
    if len(events) == 1 and primary.product_id is not None:
        route = f"/product/{primary.product_id}"
    elif len(events) == 1 and primary.brand_node_id is not None:
        # brand_sale 은 상품이 아니라 브랜드 단위 이벤트라 product_id 가 없다 —
        # 브랜드 페이지로 딥링크한다 ("/product/None" 방지).
        route = f"/brand/{primary.brand_node_id}"
    elif category == SAVED_CATEGORY:
        route = "/wishlist"
    else:
        route = "/home"
    return {
        "schema_version": 1,
        "message_id": str(message_id),
        "kind": category,
        "primary_product_id": str(primary.product_id) if primary.product_id is not None else None,
        "item_count": len(events),
        "route": route,
    }


def _category_for(event: Event) -> str:
    if event.kind == KIND_BRAND_SALE:
        return BRAND_SALE_CATEGORY
    if event.kind == KIND_BRAND_NEW:
        return BRAND_CATEGORY
    return SAVED_CATEGORY


async def _persist_outbox(
    pool: AsyncConnectionPool,
    *,
    selected: dict[UUID, list[Event]],
    inbox_only: list[Event],
    baselines: list[BaselineWrite],
    now: datetime,
) -> tuple[int, int, int, int]:
    """Atomically advance baselines and create events, messages, deliveries.

    `inbox_only` 는 푸시 동의에 막힌 찜 알림이다 — 행은 남기되 메시지/딜리버리는
    만들지 않는다 (suppressed_saved_events docstring 참조).
    """
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

        # 푸시는 막혔지만 알림함에는 남는 이벤트. 처리 완료로 표시해 두어 아웃박스가
        # 나중에 집어가지 않게 한다.
        for event in inbox_only:
            await cur.execute(
                """
                INSERT INTO ai.notifications
                    (user_id, kind, product_id, brand_node_id, payload, processed_at, suppressed_reason)
                VALUES (%s, %s, %s, %s, %s::jsonb, now(), 'consent_off')
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
            if await cur.fetchone():
                recorded += 1

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


async def _persist_brand_sale(
    pool: AsyncConnectionPool,
    *,
    events: list[Event],
    states: list[BrandSaleStateWrite],
    news: list[BrandNewsWrite],
    prefs: dict[UUID, dict[str, Any] | None],
    brand_sale_days_last_week: dict[UUID, int],
    weekly_cap: int,
    now: datetime,
) -> BrandSalePersistResult:
    """brand_sale 전용 적재 — 소식 정본이 먼저, 유저 행은 푸시 대상만.

    Phase 0 은 ai.brand_news 정본을 열고 닫는다. 팔로워가 0명이어도 여기까지는
    반드시 도달한다 — 브랜드 홈이 이 테이블을 직접 읽기 때문이다.

    Phase A 는 ai.brand_sale_state 전환 상태를 갱신한다. 이벤트 발생 여부와 무관하다.

    Phase B 는 **푸시 대상만** ai.notifications 에 적재한다. 인박스 노출은 정본이
    담당하므로(0028 read fan-out) 유저별 복제가 필요 없고, 남은 행은 아웃박스 앵커
    (notification_message_events FK) 역할만 한다.

    중복 방지가 ai.notifications 행 존재에 의존하지 않아 가능한 구조다 — brand_sale 은
    ai.brand_sale_state 의 false→true 전환 + uq_brand_news_open 이 막는다. brand_new
    처럼 ai.notifications 안티조인(_NEW_PRODUCT_SQL)을 쓰지 않으므로, 게이트에 막힌
    유저의 행을 안 남겨도 다음 감지가 오염되지 않는다.

    전 단계를 한 트랜잭션으로 원자화한다.
    """
    news_written = 0
    recorded = 0
    messages = 0
    deliveries = 0
    pushed = 0
    suppressed_consent = 0
    suppressed_cap = 0
    if not states and not events and not news:
        return BrandSalePersistResult()

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        # Phase 0 ── 브랜드 소식 정본. 유저 팬아웃보다 **먼저** 쓴다. 팔로워가 0명인
        # 브랜드도 여기까지는 반드시 도달해야 브랜드 홈이 채워진다.
        news_ids: dict[int, int] = {}
        for item in news:
            if item.opened:
                # uq_brand_news_open (brand_node_id, kind) WHERE ended_at IS NULL 이
                # 진행 중 소식을 브랜드·종류당 1건으로 강제한다. 같은 배치를 두 번
                # 돌려도 DO NOTHING 으로 흘러 소식이 늘지 않는다.
                await cur.execute(
                    """
                    INSERT INTO ai.brand_news (brand_node_id, kind, payload, started_at)
                    VALUES (%s, %s, %s::jsonb, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        item.brand_node_id,
                        item.kind,
                        json.dumps(item.payload, ensure_ascii=False, default=str),
                        now,
                    ),
                )
                row = await cur.fetchone()
                if row:
                    news_ids[item.brand_node_id] = int(row[0])
                    news_written += 1
                else:
                    # 이미 열린 소식이 있다 — 인박스 행이 그 정본을 가리키게 재조회한다.
                    await cur.execute(
                        """
                        SELECT id FROM ai.brand_news
                        WHERE brand_node_id = %s AND kind = %s AND ended_at IS NULL
                        """,
                        (item.brand_node_id, item.kind),
                    )
                    existing = await cur.fetchone()
                    if existing:
                        news_ids[item.brand_node_id] = int(existing[0])
            else:
                await cur.execute(
                    """
                    UPDATE ai.brand_news SET ended_at = %s
                    WHERE brand_node_id = %s AND kind = %s AND ended_at IS NULL
                    """,
                    (now, item.brand_node_id, item.kind),
                )
                news_written += cur.rowcount if cur.rowcount > 0 else 0

        # Phase A ── 상태 전환(on_sale/ratio) upsert. 이벤트 발생 여부와 무관하게
        # 항상 기록해야 다음 배치가 정확한 직전 상태로 false→true 를 판정한다.
        for state in states:
            await cur.execute(
                """
                INSERT INTO ai.brand_sale_state (brand_node_id, on_sale, ratio, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (brand_node_id) DO UPDATE SET
                    on_sale = EXCLUDED.on_sale,
                    ratio = EXCLUDED.ratio,
                    updated_at = EXCLUDED.updated_at
                """,
                (state.brand_node_id, state.on_sale, state.ratio, now),
            )

        # Phase B ── 푸시 대상(동의 + 주간 캡)만 ai.notifications 에 적재한다.
        #
        # 0028 이전엔 여기서 팔로워 전원의 행을 만들고 인박스가 그걸 읽었다. 이제
        # 인박스는 ai.brand_news 정본을 조회하므로(app/api/notifications.py source `b`)
        # 유저별 복제가 필요 없다. 남은 행의 유일한 역할은 아웃박스 앵커다 —
        # ai.notification_message_events.notification_id 가 이 테이블을 FK 로 건다.
        #
        # 그래서 게이트를 **적재 전에** 본다. 막힌 유저는 행 자체를 만들지 않는다.
        # 인박스 노출은 정본이 담당하므로 유실이 아니다 — 푸시만 건너뛴다.
        # (억제 사유는 행 대신 리포트 카운터로 관측한다.)
        events_by_user: dict[UUID, list[Event]] = {}
        for event in events:
            events_by_user.setdefault(event.user_id, []).append(event)

        for user_id, user_events in events_by_user.items():
            consent_ok = kind_allowed(prefs.get(user_id), KIND_BRAND_SALE)
            cap_ok = brand_sale_days_last_week.get(user_id, 0) < weekly_cap
            if not consent_ok:
                suppressed_consent += len(user_events)
                continue
            if not cap_ok:
                suppressed_cap += len(user_events)
                continue

            user_inserted: list[RecordedEvent] = []
            for event in user_events:
                await cur.execute(
                    """
                    INSERT INTO ai.notifications
                        (user_id, kind, product_id, brand_node_id, payload, brand_news_id)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        event.user_id,
                        event.kind,
                        event.product_id,
                        event.brand_node_id,
                        json.dumps(event.payload, ensure_ascii=False, default=str),
                        news_ids.get(event.brand_node_id) if event.brand_node_id is not None else None,
                    ),
                )
                row = await cur.fetchone()
                if row:
                    user_inserted.append(RecordedEvent(int(row[0]), event))
                    recorded += 1

            if not user_inserted:
                continue

            pushed += len(user_inserted)
            created_messages, created_deliveries = await _attach_brand_sale_message(cur, user_id, user_inserted, now)
            messages += created_messages
            deliveries += created_deliveries

    return BrandSalePersistResult(
        news_written=news_written,
        recorded=recorded,
        messages=messages,
        deliveries=deliveries,
        pushed=pushed,
        suppressed_consent=suppressed_consent,
        suppressed_cap=suppressed_cap,
    )


async def _persist_brand_new_news(
    pool: AsyncConnectionPool,
    *,
    counts: dict[int, tuple[str | None, int]],
    now: datetime,
) -> int:
    """브랜드 홈 "신상 N개" 요약 소식을 갱신한다. 반환: 열거나 닫은 행 수.

    세일과 달리 on/off 전환이 아니라 롤링 윈도우 집계다. 열린 소식 1건을 유지하며
    개수만 갱신하고, 윈도우가 비면 닫는다. uq_brand_news_open 이 브랜드·종류당 열린
    소식을 1건으로 강제하므로 열기/갱신이 upsert 한 방으로 끝난다.

    개수만 바뀐 갱신은 "새 소식" 이 아니므로 세지 않는다 — started_at 도 유지해
    브랜드 홈이 "언제부터 신상이 들어오기 시작했는지" 를 잃지 않는다.
    """
    written = 0
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        for brand_node_id, (brand, new_count) in counts.items():
            await cur.execute(
                """
                INSERT INTO ai.brand_news (brand_node_id, kind, payload, started_at)
                VALUES (%s, %s, %s::jsonb, %s)
                ON CONFLICT (brand_node_id, kind) WHERE ended_at IS NULL
                DO UPDATE SET payload = EXCLUDED.payload
                RETURNING (xmax = 0) AS opened
                """,
                (
                    brand_node_id,
                    NEWS_BRAND_NEW,
                    json.dumps({"brand": brand, "new_count": new_count}, ensure_ascii=False),
                    now,
                ),
            )
            row = await cur.fetchone()
            if row and row[0]:
                written += 1

        # 윈도우가 비어버린 브랜드는 소식을 닫는다 — 브랜드 홈이 3주 전 신상을 계속
        # "최근 소식" 으로 걸어두지 않게.
        await cur.execute(
            """
            UPDATE ai.brand_news SET ended_at = %s
            WHERE kind = %s AND ended_at IS NULL AND NOT (brand_node_id = ANY(%s))
            """,
            (now, NEWS_BRAND_NEW, list(counts)),
        )
        written += max(cur.rowcount, 0)

    return written


async def _attach_brand_sale_message(
    cur: Any,
    user_id: UUID,
    inserted: list[RecordedEvent],
    now: datetime,
) -> tuple[int, int]:
    """푸시 대상 brand_sale 이벤트를 유저의 당일 다이제스트 메시지에 붙인다.

    _persist_outbox 의 per-category create-or-merge 를 brand_sale 전용으로 옮긴 것이다.
    당일 같은 유저 메시지가 pending 이면 SAVED_CATEGORY 처럼 그냥 병합한다 — brand_new 의
    "overflow 는 이벤트 로그 밖에 둔다"(BRAND_CATEGORY continue) 분기는 안티조인 재시도
    전용이라 brand_sale 엔 없다. 반환: (messages_created, deliveries_created).
    """
    category = BRAND_SALE_CATEGORY
    scheduled_on, scheduled_at, expires_at = delivery_window(category, now)
    # 같은 유저·카테고리·발송일만 직렬화한다 (수동 카나리가 스케줄 배치와 안전하게 경쟁).
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
            # brand_sale 은 product_id 가 None 이라 int() 캐스팅을 하지 않는다.
            merged_events = [
                Event(user_id=row[0], kind=row[1], product_id=row[2], brand_node_id=row[3], payload=row[4])
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
        return 0, 0

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
    return 1, len(delivery_rows)


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
    include_brand_sale: bool = True,
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
    brand_new_counts: dict[int, tuple[str | None, int]] = {}
    if include_brand:
        since = cutoff - timedelta(days=settings.NOTIFY_NEW_PRODUCT_WINDOW_D)
        new_rows = await fetch_new_product_rows(
            pool,
            since=since,
            cutoff=cutoff,
            onboarding_grace_h=settings.NOTIFY_BRAND_ONBOARDING_GRACE_H,
            only_user=only_user,
        )
        brand_events = new_product_events(new_rows)
        # 브랜드 홈 요약은 알림과 별개 축이다 — 팔로우도 성별도 보지 않고, 창도 짧다.
        # 온보딩 적재분 제외만 공유한다 (양쪽 다 "신상" 을 주장하는 화면이라).
        brand_new_counts = await fetch_brand_new_counts(
            pool,
            since=cutoff - timedelta(days=settings.NOTIFY_BRAND_NEW_SUMMARY_WINDOW_D),
            cutoff=cutoff,
            onboarding_grace_h=settings.NOTIFY_BRAND_ONBOARDING_GRACE_H,
        )

    # 브랜드 세일 감지 — 상품이 아니라 브랜드 단위 이벤트다. restock/price_drop/brand_new
    # 과 달리 인박스 적재와 푸시 게이트가 분리돼(인박스는 무조건, 푸시만 동의+주간 캡)
    # 공유 select_for_delivery/_persist_outbox 경로를 타지 않고 _persist_brand_sale 이
    # 전담한다. 상태(ai.brand_sale_state) 갱신도 그 함수가 같은 트랜잭션에서 처리한다.
    sale_events: list[Event] = []
    sale_states: list[BrandSaleStateWrite] = []
    sale_news: list[BrandNewsWrite] = []
    if include_brand_sale:
        # 감지는 전체 브랜드(팔로워 무관), 팬아웃 대상만 only_user 로 좁힌다.
        sale_rows = await fetch_brand_sale_rows(pool, min_products=settings.NOTIFY_BRAND_SALE_MIN_PRODUCTS)
        sale_followers = await fetch_brand_followers(pool, only_user=only_user)
        sale_events, sale_states, sale_news = detect_brand_sale_events(
            sale_rows,
            followers=sale_followers,
            threshold=settings.NOTIFY_BRAND_SALE_THRESHOLD,
        )

    events = [*save_events, *brand_events]
    for event in events:
        report.detected[event.kind] += 1
    for event in sale_events:
        report.detected[event.kind] += 1

    # prefs·캡 조회 대상엔 sale_events 유저까지 포함한다 — brand_sale Phase B 푸시
    # 게이트가 동의(prefs)와 전용 주간 캡을 참조한다.
    user_ids = sorted({e.user_id for e in (*events, *sale_events)}, key=str)
    prefs = await fetch_preferences(pool, user_ids)
    brand_days = await fetch_brand_new_days_last_week(pool, user_ids)
    brand_sale_days = await fetch_brand_sale_days_last_week(pool, user_ids)
    # select_for_delivery 는 restock/price_drop/brand_new 만 다룬다 — brand_sale 은
    # 인박스 무조건 적재 + 푸시 게이트가 달라 _persist_brand_sale 이 따로 처리한다.
    selected = select_for_delivery(
        events,
        prefs=prefs,
        brand_new_days_last_week=brand_days,
        weekly_cap=settings.NOTIFY_BRAND_NEW_WEEKLY_CAP,
        max_brand_items=settings.NOTIFY_BRAND_NEW_MAX_ITEMS,
        max_per_brand=settings.NOTIFY_BRAND_NEW_MAX_PER_BRAND,
    )
    # 동의에 막힌 이벤트는 푸시만 건너뛰고 인박스에는 남긴다. 캡·상한에 막힌 몫은
    # 여기 없다 — 재시도 대상이라 기록하면 안 된다 (consent_suppressed_events 참조).
    inbox_only = consent_suppressed_events(
        events,
        prefs=prefs,
        max_brand_items=settings.NOTIFY_BRAND_NEW_MAX_ITEMS,
        max_per_brand=settings.NOTIFY_BRAND_NEW_MAX_PER_BRAND,
    )
    if limit is not None:
        allowed_users = set(list(selected)[: max(0, limit)])
        selected = {user_id: value for user_id, value in selected.items() if user_id in allowed_users}
        baselines = [baseline for baseline in baselines if baseline.user_id in allowed_users]
        inbox_only = [event for event in inbox_only if event.user_id in allowed_users]
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
    ) = await _persist_outbox(pool, selected=selected, inbox_only=inbox_only, baselines=baselines, now=cutoff)
    report.push_suppressed_consent += len(inbox_only)

    # brand_sale 은 별도 경로 — Phase 0 브랜드 소식 정본, Phase A 상태 전환 upsert,
    # Phase B 동의·전용 주간 캡 통과분만 유저 행 + APNs 푸시.
    # 카운트를 아웃박스와 합산한다.
    sale = await _persist_brand_sale(
        pool,
        events=sale_events,
        states=sale_states,
        news=sale_news,
        prefs=prefs,
        brand_sale_days_last_week=brand_sale_days,
        weekly_cap=settings.NOTIFY_BRAND_SALE_WEEKLY_CAP,
        now=cutoff,
    )
    report.brand_sale_news = sale.news_written
    # 브랜드 홈 "신상 N개" 요약. 알림 경로와 독립이라 팬아웃/푸시가 붙지 않는다.
    if include_brand:
        report.brand_new_news = await _persist_brand_new_news(pool, counts=brand_new_counts, now=cutoff)
    report.recorded += sale.recorded
    report.messages_queued += sale.messages
    report.deliveries_queued += sale.deliveries
    report.selected[KIND_BRAND_SALE] += sale.pushed
    report.push_suppressed_consent += sale.suppressed_consent
    report.push_suppressed_cap += sale.suppressed_cap

    if include_saved and not include_brand:
        job_name = SAVED_JOB
    elif include_brand and not include_saved:
        job_name = BRAND_JOB
    else:
        job_name = NOTIFY_JOB
    await record_run(pool, job_name, cutoff)
    logger.info(report.as_line())
    return report
