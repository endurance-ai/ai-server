"""발송 정책 — 동의 · 주간 캡 · 다이제스트 문구."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.api.devices import _DEFAULT_NOTIFICATION_SETTINGS, _with_defaults
from app.services.notifications import (
    KIND_BRAND_NEW,
    KIND_PRICE_DROP,
    KIND_RESTOCK,
    Event,
    build_digest,
    delivery_window,
    kind_allowed,
    select_for_delivery,
)

USER = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")


def _event(kind: str, product_id: int, *, user_id: UUID = USER, **payload) -> Event:
    return Event(
        user_id=user_id,
        kind=kind,
        product_id=product_id,
        payload={"brand": "MAISON", "name": "린넨 셔츠", "price": 80_000.0, **payload},
    )


def _select(events, **kw):
    params = {
        "prefs": {},
        "brand_new_days_last_week": {},
        "weekly_cap": 3,
        "max_brand_items": 5,
    }
    params.update(kw)
    return select_for_delivery(events, **params)


# ── 동의 ─────────────────────────────────────────────────────────────────────


def test_missing_keys_are_opt_in():
    assert kind_allowed(None, KIND_RESTOCK) is True
    assert kind_allowed({"system": True}, KIND_PRICE_DROP) is True


def test_system_is_the_master_switch_for_every_kind():
    prefs = {"system": False, "release_alerts": True, "restock": True}
    assert kind_allowed(prefs, KIND_RESTOCK) is False
    assert kind_allowed(prefs, KIND_PRICE_DROP) is False
    assert kind_allowed(prefs, KIND_BRAND_NEW) is False


def test_marketing_switch_only_additionally_gates_brand_news():
    prefs = {"system": True, "release_alerts": False}
    assert kind_allowed(prefs, KIND_RESTOCK) is True
    assert kind_allowed(prefs, KIND_PRICE_DROP) is True
    assert kind_allowed(prefs, KIND_BRAND_NEW) is False


def test_explicit_false_excludes_only_that_kind():
    prefs = {USER: {"price_drop": False}}
    selected = _select([_event(KIND_PRICE_DROP, 1), _event(KIND_RESTOCK, 2)], prefs=prefs)

    assert [e.kind for e in selected[USER]] == [KIND_RESTOCK]


def test_user_with_everything_off_disappears():
    prefs = {USER: {"restock": False, "price_drop": False, "brand_new_product": False}}
    assert _select([_event(KIND_RESTOCK, 1), _event(KIND_BRAND_NEW, 2)], prefs=prefs) == {}


# ── 캡 ───────────────────────────────────────────────────────────────────────


def test_brand_new_weekly_cap_drops_only_brand_events():
    events = [_event(KIND_BRAND_NEW, 1), _event(KIND_RESTOCK, 2)]
    selected = _select(events, brand_new_days_last_week={USER: 3}, weekly_cap=3)

    assert [e.kind for e in selected[USER]] == [KIND_RESTOCK]


def test_brand_new_under_the_cap_passes():
    selected = _select([_event(KIND_BRAND_NEW, 1)], brand_new_days_last_week={USER: 2}, weekly_cap=3)
    assert [e.kind for e in selected[USER]] == [KIND_BRAND_NEW]


def test_digest_size_is_capped_per_run():
    events = [_event(KIND_BRAND_NEW, pid) for pid in range(20)]
    selected = _select(events, max_brand_items=5)

    assert len(selected[USER]) == 5
    # 쿼리 순서(최신순)를 유지한다.
    assert [e.product_id for e in selected[USER]] == [0, 1, 2, 3, 4]


def test_caps_are_per_user():
    events = [_event(KIND_BRAND_NEW, 1), _event(KIND_BRAND_NEW, 2, user_id=OTHER)]
    selected = _select(events, brand_new_days_last_week={USER: 3}, weekly_cap=3)

    assert USER not in selected
    assert len(selected[OTHER]) == 1


def test_saved_kinds_lead_the_digest():
    events = [_event(KIND_BRAND_NEW, 1), _event(KIND_PRICE_DROP, 2), _event(KIND_RESTOCK, 3)]
    selected = _select(events)

    assert [e.kind for e in selected[USER]] == [KIND_RESTOCK, KIND_PRICE_DROP, KIND_BRAND_NEW]


# ── 다이제스트 ───────────────────────────────────────────────────────────────


def test_single_restock_digest():
    digest = build_digest([_event(KIND_RESTOCK, 7)], notification_ids=[42])

    assert digest.title == "재입고 알림"
    assert digest.body == "MAISON 린넨 셔츠 다시 입고됐어요"
    assert digest.data == {"kind": KIND_RESTOCK, "product_ids": [7], "notification_ids": [42]}


def test_price_drop_digest_shows_the_percentage():
    digest = build_digest([_event(KIND_PRICE_DROP, 7, drop_pct=23)])
    assert digest.body == "MAISON 린넨 셔츠 가격이 23% 내려갔어요"


def test_brand_new_digest_collapses_to_a_count():
    digest = build_digest([_event(KIND_BRAND_NEW, i) for i in range(4)])

    assert digest.title == "관심 브랜드 새 상품"
    assert digest.body == "MAISON에 새 상품 4개가 올라왔어요"


def test_multi_brand_new_digest_drops_the_brand_name():
    events = [_event(KIND_BRAND_NEW, 1), _event(KIND_BRAND_NEW, 2, brand="OTHER")]
    assert build_digest(events).body == "관심 브랜드에 새 상품 2개가 올라왔어요"


def test_mixed_digest_is_one_push():
    digest = build_digest([_event(KIND_RESTOCK, 1), _event(KIND_BRAND_NEW, 2)], notification_ids=[1, 2])

    assert digest.title == "kiko 알림"
    assert digest.body == "MAISON 린넨 셔츠 다시 입고됐어요 외 1건"
    assert digest.data["product_ids"] == [1, 2]


def test_digest_survives_missing_product_metadata():
    event = Event(user_id=USER, kind=KIND_RESTOCK, product_id=1, payload={})
    assert build_digest([event]).body == "찜한 상품 다시 입고됐어요"


# ── 설정 API 회귀 가드 ────────────────────────────────────────────────────────


def test_new_categories_do_not_disturb_the_existing_three():
    """모바일 notifications.tsx 는 system / release_alerts 두 토글에 매핑돼 있다."""
    merged = _with_defaults({"system": False, "release_alerts": False, "taste_push": True})

    assert merged["system"] is False
    assert merged["release_alerts"] is False
    assert merged["taste_push"] is True
    assert merged["restock"] is True
    assert set(merged) == set(_DEFAULT_NOTIFICATION_SETTINGS)


def test_absent_settings_fall_back_to_full_opt_in():
    assert _with_defaults(None) == _DEFAULT_NOTIFICATION_SETTINGS


# ── 발송 시간 ───────────────────────────────────────────────────────────────


def test_saved_digest_waits_until_0930_kst_before_quiet_hours_end():
    scheduled_on, scheduled_at, expires_at = delivery_window(
        "saved_product_digest",
        datetime(2026, 8, 1, 23, 30, tzinfo=UTC),  # 08:30 KST
    )

    assert scheduled_on.isoformat() == "2026-08-02"
    assert scheduled_at == datetime(2026, 8, 2, 0, 30, tzinfo=UTC)
    assert expires_at == datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_after_2100_kst_rolls_to_the_next_morning():
    scheduled_on, scheduled_at, _expires_at = delivery_window(
        "saved_product_digest",
        datetime(2026, 8, 2, 12, 0, tzinfo=UTC),  # 21:00 KST
    )

    assert scheduled_on.isoformat() == "2026-08-03"
    assert scheduled_at == datetime(2026, 8, 3, 0, 30, tzinfo=UTC)


def test_delayed_brand_worker_catches_up_during_active_hours():
    now = datetime(2026, 8, 2, 3, 30, tzinfo=UTC)  # 12:30 KST, planned 11:00 missed
    scheduled_on, scheduled_at, _expires_at = delivery_window("brand_new_digest", now)

    assert scheduled_on.isoformat() == "2026-08-02"
    assert scheduled_at == now
