"""SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-CALLBACK-001 — parse_callback.

Strict `clarify:<axis>:<value>` parser, deterministic (no LLM). Complements
test_clarify_axis.py (which covers pick_clarify_axis).
"""

from __future__ import annotations

import pytest

from app.channels.clarify import ClarifyAxis, parse_callback
from app.channels.clarify_values import SKIP_VALUE

# ── malformed input → None ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cb",
    [
        "",
        "crit:more:0",  # wrong prefix
        "clarify:fit",  # only 2 parts
        "clarify::value",  # empty axis
        "clarify:fit:",  # empty value
        "clarify:bogus_axis:casual",  # unknown axis
        "clarify:fit:not_a_real_fit",  # unknown value
    ],
)
def test_malformed_callbacks_return_none(cb):
    assert parse_callback(cb) is None


def test_non_string_returns_none():
    assert parse_callback(None) is None  # type: ignore[arg-type]
    assert parse_callback(123) is None  # type: ignore[arg-type]


# ── skip → terminating delta ────────────────────────────────────────────────


def test_skip_value_is_valid_for_any_axis():
    delta = parse_callback(f"clarify:formality:{SKIP_VALUE}")
    assert delta is not None
    assert delta.axis == ClarifyAxis.FORMALITY
    assert delta.is_skip is True
    assert delta.value == SKIP_VALUE


# ── valid option → populated delta ──────────────────────────────────────────


def test_category_pick_top_populates_overrides():
    delta = parse_callback("clarify:category_pick:top")
    assert delta is not None
    assert delta.axis == ClarifyAxis.CATEGORY_PICK
    assert delta.value == "top"
    assert delta.is_skip is False
    assert delta.keywords_to_boost == ["top"]
    assert delta.subcategory_override == "top"
    assert delta.searchQueryKo_augment == "상의"
    assert delta.raw_callback == "clarify:category_pick:top"


def test_formality_casual_boosts_keywords():
    delta = parse_callback("clarify:formality:casual")
    assert delta is not None
    assert delta.axis == ClarifyAxis.FORMALITY
    assert delta.keywords_to_boost == ["casual"]


def test_axis_and_value_are_lowercased():
    delta = parse_callback("clarify:CATEGORY_PICK:TOP")
    assert delta is not None
    assert delta.axis == ClarifyAxis.CATEGORY_PICK
    assert delta.value == "top"
