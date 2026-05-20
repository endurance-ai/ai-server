"""apply_price_filter — unit tests.

Covers: both bounds, single bound, no bounds passthrough, missing-price drop,
non-numeric price rejection, dict-shaped candidates, integer vs float bounds.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.tools.search_products import apply_price_filter


@dataclass
class _Cand:
    id: str
    price: float | None


def _ids(cands):
    return [c.id if hasattr(c, "id") else c["id"] for c in cands]


# ─── passthrough / no bounds ───────────────────────────────────────────────


def test_no_bounds_returns_unchanged():
    cands = [_Cand("a", 1000), _Cand("b", None), _Cand("c", 50000)]
    out = apply_price_filter(cands, None, None)
    assert out is cands or out == cands  # identity-or-equal passthrough


# ─── max_price ─────────────────────────────────────────────────────────────


def test_max_price_drops_above():
    cands = [_Cand("a", 30000), _Cand("b", 80000), _Cand("c", 200000)]
    out = apply_price_filter(cands, None, 100000)
    assert _ids(out) == ["a", "b"]


def test_max_price_inclusive_at_bound():
    cands = [_Cand("a", 100000), _Cand("b", 100001)]
    out = apply_price_filter(cands, None, 100000)
    assert _ids(out) == ["a"]


# ─── min_price ─────────────────────────────────────────────────────────────


def test_min_price_drops_below():
    cands = [_Cand("a", 30000), _Cand("b", 80000), _Cand("c", 200000)]
    out = apply_price_filter(cands, 50000, None)
    assert _ids(out) == ["b", "c"]


def test_min_price_inclusive_at_bound():
    cands = [_Cand("a", 49999), _Cand("b", 50000)]
    out = apply_price_filter(cands, 50000, None)
    assert _ids(out) == ["b"]


# ─── both bounds ───────────────────────────────────────────────────────────


def test_both_bounds_inclusive_window():
    cands = [
        _Cand("a", 30000),  # below
        _Cand("b", 50000),  # at min
        _Cand("c", 75000),  # in
        _Cand("d", 100000),  # at max
        _Cand("e", 150000),  # above
    ]
    out = apply_price_filter(cands, 50000, 100000)
    assert _ids(out) == ["b", "c", "d"]


# ─── missing / unparseable price ───────────────────────────────────────────


def test_missing_price_dropped_when_any_bound_set():
    """Conservative — explicit user budget means un-priced rows are out."""
    cands = [_Cand("a", 50000), _Cand("b", None), _Cand("c", 80000)]
    out = apply_price_filter(cands, None, 100000)
    assert _ids(out) == ["a", "c"]


def test_unparseable_price_dropped():
    @dataclass
    class _BadCand:
        id: str
        price: object

    cands = [_BadCand("a", "오십만원"), _BadCand("b", 80000)]
    out = apply_price_filter(cands, None, 100000)
    assert _ids(out) == ["b"]


# ─── dict-shaped candidates ────────────────────────────────────────────────


def test_dict_candidates_supported():
    cands = [
        {"id": "a", "price": 30000},
        {"id": "b", "price": 80000},
        {"id": "c", "price": 120000},
    ]
    out = apply_price_filter(cands, None, 100000)
    assert _ids(out) == ["a", "b"]


# ─── float bounds ──────────────────────────────────────────────────────────


def test_float_bounds_accepted():
    cands = [_Cand("a", 50000.50), _Cand("b", 50000.49)]
    out = apply_price_filter(cands, 50000.50, None)
    assert _ids(out) == ["a"]


# ─── empty input ───────────────────────────────────────────────────────────


def test_empty_input_returns_empty():
    assert apply_price_filter([], None, 100000) == []
    assert apply_price_filter([], 50000, 100000) == []
