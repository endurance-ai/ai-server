from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.services.edit_shop_rankings import RankingProduct, build_ranking, what100_is_fresh


def _products(count: int) -> list[RankingProduct]:
    return [RankingProduct(product_id=i, product_no=1000 + i, brand=f"brand-{i % 4}") for i in range(1, count + 1)]


def test_slowsteadyclub_keeps_twenty_what100_rows_then_daily_tail() -> None:
    products = _products(30)
    what100 = {1000 + i: i for i in range(1, 26)}
    ranked, source, _ = build_ranking(
        products,
        generated_for=date(2026, 9, 15),
        platform="slowsteadyclub",
        gender="women",
        category="all",
        what100_ranks=what100,
    )
    assert source == "what100_plus_shuffle"
    assert [item.product_no for item in ranked[:20]] == [1000 + i for i in range(1, 21)]


def test_what100_only_defers_the_third_consecutive_brand() -> None:
    products = [
        RankingProduct(1, 101, "A"),
        RankingProduct(2, 102, "A"),
        RankingProduct(3, 103, "A"),
        RankingProduct(4, 104, "B"),
        RankingProduct(5, 105, "C"),
    ]
    ranked, _, _ = build_ranking(
        products,
        generated_for=date(2026, 9, 15),
        platform="slowsteadyclub",
        gender="women",
        category="all",
        what100_ranks={item.product_no: index for index, item in enumerate(products, 1) if item.product_no},
    )
    assert [item.product_id for item in ranked] == [1, 2, 4, 3, 5]


def test_popular_threshold_switches_at_500_and_uses_distinct_user_scores() -> None:
    products = _products(4)
    scores = {1: 1.0, 2: 99.0}
    below, below_source, _ = build_ranking(
        products,
        generated_for=date(2026, 9, 15),
        platform="8division",
        gender="men",
        category="all",
        signal_scores=scores,
        signal_count=499,
    )
    at, at_source, _ = build_ranking(
        products,
        generated_for=date(2026, 9, 15),
        platform="8division",
        gender="men",
        category="all",
        signal_scores=scores,
        signal_count=500,
    )
    assert below_source == "daily_shuffle"
    assert at_source == "popular"
    assert at[0].product_id == 2
    assert below != at


def test_daily_shuffle_is_stable_and_brand_run_is_limited_when_possible() -> None:
    products = [RankingProduct(i, i, "same" if i <= 5 else f"other-{i}") for i in range(1, 10)]
    first, _, _ = build_ranking(
        products,
        generated_for=date(2026, 9, 15),
        platform="kith",
        gender="women",
        category="all",
    )
    second, _, _ = build_ranking(
        products,
        generated_for=date(2026, 9, 15),
        platform="kith",
        gender="women",
        category="all",
    )
    assert first == second
    assert all(
        not (first[index].brand == first[index - 1].brand == first[index - 2].brand) for index in range(2, len(first))
    )


def test_what100_becomes_stale_after_48_hours() -> None:
    now = datetime(2026, 9, 15, tzinfo=UTC)
    assert what100_is_fresh(now - timedelta(hours=48), now)
    assert not what100_is_fresh(now - timedelta(hours=48, seconds=1), now)
