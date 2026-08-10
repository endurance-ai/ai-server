"""찜 기반 감지(재입고 / 가격 하락) 순수 판정 — DB 없음."""

from __future__ import annotations

from uuid import UUID

from app.services.notifications import (
    KIND_BRAND_NEW,
    KIND_BRAND_SALE,
    KIND_PRICE_DROP,
    KIND_RESTOCK,
    BrandSaleRow,
    Event,
    SavedRow,
    detect_brand_sale_events,
    detect_save_events,
    pick_brand_new,
)

USER = UUID("11111111-1111-1111-1111-111111111111")
OTHER = UUID("22222222-2222-2222-2222-222222222222")


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


# ── 신상 하루치 선별 (순수 판정) ─────────────────────────────────────────────


def _new(product_id: int, brand_node_id: int) -> Event:
    return Event(user_id=USER, kind=KIND_BRAND_NEW, product_id=product_id, brand_node_id=brand_node_id)


def test_one_brand_cannot_take_the_whole_day():
    """products.created_at 은 적재 시각이라, 그대로 자르면 마지막에 크롤된 브랜드가 독식한다."""
    # 최신순 입력: 브랜드 10 이 앞을 다 차지한 상태 (통짜 적재된 브랜드).
    events = [_new(i, 10) for i in range(1, 6)] + [_new(6, 20), _new(7, 30)]

    picked = pick_brand_new(events, max_items=5, max_per_brand=2)

    # 자르기만 했다면 전부 브랜드 10 이었다. 상한이 다른 두 브랜드에 자리를 만든다.
    assert [e.brand_node_id for e in picked] == [10, 10, 10, 20, 30]
    # 최신순(입력 순서)이 유지된다 — build_digest 가 events[0] 로 대표를 고른다.
    assert [e.product_id for e in picked] == [1, 2, 3, 6, 7]


def test_single_brand_follower_still_gets_a_full_day():
    """다양성 장치가 물량 축소로 변질되면 안 된다 — 슬롯이 남으면 상한 초과분으로 채운다."""
    events = [_new(i, 10) for i in range(1, 9)]

    picked = pick_brand_new(events, max_items=5, max_per_brand=2)

    assert len(picked) == 5
    assert [e.product_id for e in picked] == [1, 2, 3, 4, 5]


def test_backfill_keeps_newest_first_order():
    events = [_new(1, 10), _new(2, 10), _new(3, 10), _new(4, 20)]

    picked = pick_brand_new(events, max_items=4, max_per_brand=2)

    # 브랜드 20 을 먼저 확보한 뒤 남은 슬롯을 브랜드 10 초과분으로 채우되, 순서는 원복한다.
    assert [e.product_id for e in picked] == [1, 2, 3, 4]


def test_fewer_candidates_than_the_cap_is_returned_whole():
    events = [_new(1, 10), _new(2, 20)]
    assert pick_brand_new(events, max_items=5, max_per_brand=2) == events


# ── 브랜드 세일 감지 (순수 판정) ──────────────────────────────────────────────


def _sale_row(**kw) -> BrandSaleRow:
    base = {
        "brand_node_id": 100,
        "brand": "MAISON",
        "sale_count": 4,
        "total_count": 10,
        "prev_on_sale": False,
        "max_discount_pct": 0.45,  # 가장 깊은 개별 상품 할인 45% — 문구용
    }
    base.update(kw)
    return BrandSaleRow(**base)


_FOLLOWED = {100: (USER,)}


def test_brand_sale_fires_on_false_to_true_transition_per_follower():
    events, states, news = detect_brand_sale_events([_sale_row()], followers={100: (USER, OTHER)}, threshold=0.30)

    assert {e.user_id for e in events} == {USER, OTHER}
    assert all(e.kind == KIND_BRAND_SALE for e in events)
    assert all(e.product_id is None for e in events)
    assert all(e.brand_node_id == 100 for e in events)
    assert events[0].payload["sale_pct"] == 40
    # 문구가 실제로 쓰는 값 — 카탈로그 커버리지(sale_pct)가 아니라 최대 개별 할인율.
    assert events[0].payload["max_discount_pct"] == 45
    # 상태는 항상 현재 비율/온세일로 기록된다.
    assert len(states) == 1
    assert states[0].on_sale is True
    assert states[0].ratio == 0.4
    # 소식 정본은 팔로워 수와 무관하게 브랜드당 1건이다.
    assert len(news) == 1
    assert news[0].brand_node_id == 100
    assert news[0].opened is True
    assert news[0].payload["max_discount_pct"] == 45


def test_brand_already_on_sale_does_not_refire():
    events, states, news = detect_brand_sale_events([_sale_row(prev_on_sale=True)], followers=_FOLLOWED, threshold=0.30)

    assert events == []
    # 연속 세일 기간엔 새 소식을 열지도 닫지도 않는다.
    assert news == []
    # 연속 세일 기간에도 상태는 갱신해 둔다 (다음 배치의 전환 판정 기준).
    assert states[0].on_sale is True


def test_brand_below_threshold_does_not_fire_and_marks_not_on_sale():
    events, states, news = detect_brand_sale_events(
        [_sale_row(sale_count=2, total_count=10)], followers=_FOLLOWED, threshold=0.30
    )

    assert events == []
    # 애초에 세일 중이 아니었으니 닫을 소식도 없다.
    assert news == []
    assert states[0].on_sale is False
    assert states[0].ratio == 0.2


def test_brand_at_exact_threshold_fires():
    events, _, news = detect_brand_sale_events(
        [_sale_row(sale_count=3, total_count=10)], followers=_FOLLOWED, threshold=0.30
    )
    assert [e.kind for e in events] == [KIND_BRAND_SALE]
    assert [n.opened for n in news] == [True]


def test_brand_dropping_back_below_threshold_clears_state_so_it_can_fire_again():
    # on_sale 이었다가 비율이 떨어지면 상태가 false 로 내려가고,
    off, states, news = detect_brand_sale_events(
        [_sale_row(prev_on_sale=True, sale_count=1, total_count=10)], followers=_FOLLOWED, threshold=0.30
    )
    assert off == []
    assert states[0].on_sale is False
    # 진행 중이던 소식은 닫힌다 — 브랜드 홈이 끝난 세일을 계속 걸어두지 않는다.
    assert [n.opened for n in news] == [False]

    # 다음 세일에서 다시 발동한다 (false→true).
    again, _, again_news = detect_brand_sale_events(
        [_sale_row(prev_on_sale=False)], followers=_FOLLOWED, threshold=0.30
    )
    assert [e.kind for e in again] == [KIND_BRAND_SALE]
    assert [n.opened for n in again_news] == [True]


def test_brand_with_no_products_is_safe():
    events, states, news = detect_brand_sale_events(
        [_sale_row(sale_count=0, total_count=0)], followers=_FOLLOWED, threshold=0.30
    )
    assert events == []
    assert news == []
    assert states[0].on_sale is False
    assert states[0].ratio == 0.0


def test_brand_with_no_followers_still_writes_news_and_state():
    """0027 의 핵심 — 팔로워가 0명이어도 소식은 만들어진다.

    브랜드 홈은 비로그인도 보는 페이지라, 소식 생성이 팔로우 여부에 종속되면
    아무도 팔로우하지 않은 브랜드의 홈은 영영 비어 있게 된다.
    """
    events, states, news = detect_brand_sale_events([_sale_row()], followers={}, threshold=0.30)
    assert events == []
    assert states[0].on_sale is True
    assert [n.opened for n in news] == [True]
    assert news[0].payload["brand"] == "MAISON"
