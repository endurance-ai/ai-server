"""SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-CALLBACK-001 — parse_callback 단위 테스트."""

from __future__ import annotations

import pytest

from app.channels.clarify import ClarifyAxis, ClarifyDelta, parse_callback
from app.channels.clarify_values import AXIS_OPTIONS


class TestParseCallbackHappyPath:
    def test_formality_casual(self):
        delta = parse_callback("clarify:formality:casual")
        assert isinstance(delta, ClarifyDelta)
        assert delta.axis == ClarifyAxis.FORMALITY
        assert delta.value == "casual"
        assert delta.is_skip is False
        assert "casual" in delta.keywords_to_boost
        assert delta.searchQueryKo_augment == "캐주얼"
        assert delta.raw_callback == "clarify:formality:casual"

    def test_fit_oversize(self):
        delta = parse_callback("clarify:fit:oversize")
        assert delta is not None
        assert delta.axis == ClarifyAxis.FIT
        assert "oversized" in delta.keywords_to_boost

    def test_category_pick_top(self):
        delta = parse_callback("clarify:category_pick:top")
        assert delta is not None
        assert delta.axis == ClarifyAxis.CATEGORY_PICK
        assert delta.subcategory_override == "top"

    def test_skip_value_returns_skip_delta(self):
        delta = parse_callback("clarify:formality:skip")
        assert delta is not None
        assert delta.is_skip is True
        assert delta.value == "skip"
        assert not delta.keywords_to_boost
        assert delta.subcategory_override is None


class TestParseCallbackMalformed:
    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "clarify:",
            "clarify:formality",  # value 누락
            "clarify::casual",  # axis 빈
            "clarify:formality:",  # value 빈
            "crit:more:0",  # 다른 prefix
            "formality:casual",  # prefix 누락
            "clarify:unknown_axis:casual",
            "clarify:formality:nonexistent_value",
            "clarify:formality:casual:extra",  # extra 토큰 — strip 후 다른 value 로 평가됨
        ],
    )
    def test_malformed_returns_none(self, bad: str):
        assert parse_callback(bad) is None

    def test_none_input_returns_none(self):
        # type: ignore[arg-type]
        assert parse_callback(None) is None  # type: ignore[arg-type]

    def test_non_string_input_returns_none(self):
        # type: ignore[arg-type]
        assert parse_callback(12345) is None  # type: ignore[arg-type]


class TestRoundTripAllAxes:
    """모든 (axis, value) 조합이 round-trip 으로 다시 ClarifyDelta 가 됨."""

    def test_all_known_combinations_roundtrip(self):
        for axis_val, opts in AXIS_OPTIONS.items():
            for opt in opts:
                payload = f"clarify:{axis_val}:{opt.value}"
                delta = parse_callback(payload)
                assert delta is not None, f"{payload} parse 실패"
                assert delta.axis.value == axis_val
                assert delta.value == opt.value
                assert delta.keywords_to_boost == list(opt.keywords_to_boost)
                assert delta.subcategory_override == opt.subcategory_override
                assert delta.searchQueryKo_augment == opt.searchQueryKo_augment

    def test_skip_roundtrip_for_each_axis(self):
        for axis_val in AXIS_OPTIONS:
            payload = f"clarify:{axis_val}:skip"
            delta = parse_callback(payload)
            assert delta is not None
            assert delta.is_skip is True
            assert delta.axis.value == axis_val
