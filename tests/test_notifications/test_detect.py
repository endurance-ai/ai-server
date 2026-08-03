"""찜 기반 감지(재입고 / 가격 하락) 순수 판정 — DB 없음."""

from __future__ import annotations

from uuid import UUID

from app.services.notifications import (
    KIND_PRICE_DROP,
    KIND_RESTOCK,
    SavedRow,
    detect_save_events,
)

USER = UUID("11111111-1111-1111-1111-111111111111")


def _row(**kw) -> SavedRow:
    base = {
        "user_id": USER,
        "product_id": 1,
        "brand": "MAISON",
        "name": "린넨 셔츠",
        "price": 100_000.0,
        "in_stock": True,
        "has_baseline": True,
        "baseline_price": 100_000.0,
        "baseline_in_stock": True,
    }
    base.update(kw)
    return SavedRow(**base)


def test_missing_baseline_is_backfilled_without_notifying():
    events, baselines = detect_save_events([_row(has_baseline=False, baseline_price=None)], threshold=0.15)

    assert events == []
    assert len(baselines) == 1
    assert baselines[0].price == 100_000.0
    assert baselines[0].in_stock is True


def test_restock_fires_when_baseline_was_out_of_stock():
    events, baselines = detect_save_events([_row(in_stock=True, baseline_in_stock=False)], threshold=0.15)

    assert [e.kind for e in events] == [KIND_RESTOCK]
    # 기준 재고는 현재값을 따라간다 — 같은 재입고로 두 번 울리지 않는다.
    assert baselines[0].in_stock is True
    assert baselines[0].price == 100_000.0

    again, _ = detect_save_events([_row(in_stock=True, baseline_in_stock=True)], threshold=0.15)
    assert again == []


def test_going_out_of_stock_records_baseline_but_does_not_notify():
    events, baselines = detect_save_events([_row(in_stock=False, baseline_in_stock=True)], threshold=0.15)

    assert events == []
    assert baselines[0].in_stock is False


def test_price_drop_respects_threshold():
    at_threshold, _ = detect_save_events([_row(price=85_000.0)], threshold=0.15)
    assert [e.kind for e in at_threshold] == [KIND_PRICE_DROP]
    assert at_threshold[0].payload["drop_pct"] == 15

    just_under, baselines = detect_save_events([_row(price=86_000.0)], threshold=0.15)
    assert just_under == []
    assert baselines == []


def test_price_drop_moves_baseline_down_so_it_does_not_repeat():
    events, baselines = detect_save_events([_row(price=80_000.0)], threshold=0.15)
    assert [e.kind for e in events] == [KIND_PRICE_DROP]
    assert baselines[0].price == 80_000.0

    # 새 기준가(80,000) 대비로는 아직 하락이 아니다.
    again, _ = detect_save_events([_row(price=80_000.0, baseline_price=80_000.0)], threshold=0.15)
    assert again == []


def test_price_rise_does_not_raise_the_baseline():
    """올랐다가 제자리로 돌아온 것을 하락으로 오인하면 안 된다."""
    events, baselines = detect_save_events([_row(price=150_000.0)], threshold=0.15)

    assert events == []
    assert baselines == []


def test_slow_multi_day_drop_is_caught_against_the_save_time_baseline():
    """하루 3%씩 6일(0.97^6 = 83.3%) — 직전 크롤 대비 델타로는 못 잡는 케이스."""
    events, _ = detect_save_events([_row(price=83_297.0)], threshold=0.15)
    assert [e.kind for e in events] == [KIND_PRICE_DROP]


def test_restock_and_price_drop_can_fire_together():
    events, baselines = detect_save_events(
        [_row(price=50_000.0, in_stock=True, baseline_in_stock=False)], threshold=0.15
    )

    assert {e.kind for e in events} == {KIND_RESTOCK, KIND_PRICE_DROP}
    assert baselines[0].price == 50_000.0
    assert baselines[0].in_stock is True


def test_zero_or_missing_prices_are_ignored():
    events, _ = detect_save_events([_row(price=None), _row(baseline_price=0.0, price=1.0)], threshold=0.15)
    assert events == []
