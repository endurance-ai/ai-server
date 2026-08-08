"""SPEC-CLARIFY-CARDS-001 — clarify_values 매핑 표 검증.

REQ-CLARIFY-VALUE-MAPPING-001: 모든 enum 값이 매핑 표에 존재해야 한다.
REQ-CLARIFY-CARD-003: 모든 라벨이 가독성 한계 내 + 이모지 금지 + Telegram
callback_data 64바이트 한도(R3) 검증.
"""

from __future__ import annotations

import pytest

from app.channels.clarify import ClarifyAxis
from app.channels.clarify_values import (
    AXIS_OPTIONS,
    AXIS_PROMPTS_KO,
    SKIP_LABEL_KO,
    SKIP_VALUE,
    get_option,
    get_options,
)

# REQ-CLARIFY-CARD-003 hard limits.
LABEL_HARD_BYTES = 50  # Telegram inline button label limit (실제 ~64; 보수적으로 50).
LABEL_RECOMMENDED_CHARS = 16
CALLBACK_HARD_BYTES = 64


class TestEnumCompleteness:
    """모든 ClarifyAxis enum 값이 매핑 표에 존재."""

    def test_all_axes_have_options(self):
        for axis in ClarifyAxis:
            opts = AXIS_OPTIONS.get(axis.value)
            assert opts is not None, f"axis={axis.value} 매핑 표 누락"
            assert len(opts) >= 3, f"axis={axis.value} 옵션이 3개 미만 (REQ-CLARIFY-CARD-001)"
            if axis is ClarifyAxis.COLOR:
                # color 는 개인화 풀 — 전체 어휘는 버튼 한계를 넘을 수 있고, 실제
                # 노출은 personalized_color_options(k=6)가 캡한다 (TestColorPersonalization).
                continue
            assert len(opts) <= 5, f"axis={axis.value} 옵션이 5개 초과 (skip 포함 시 6 → 한계 위반)"

    def test_all_axes_have_prompt(self):
        for axis in ClarifyAxis:
            assert axis.value in AXIS_PROMPTS_KO, f"axis={axis.value} 프롬프트 누락"
            prompt = AXIS_PROMPTS_KO[axis.value]
            assert prompt, "프롬프트 빈 문자열 금지"
            assert len(prompt) <= 60, f"axis={axis.value} 프롬프트가 60자 초과: {prompt}"


class TestLabelGuards:
    """REQ-CLARIFY-CARD-003 — 라벨 길이 + 이모지 금지."""

    @pytest.mark.parametrize("axis", [a.value for a in ClarifyAxis])
    def test_labels_within_hard_limit(self, axis: str):
        for opt in get_options(axis):
            byte_len = len(opt.label_ko.encode("utf-8"))
            assert byte_len <= LABEL_HARD_BYTES, (
                f"axis={axis} value={opt.value} label='{opt.label_ko}' bytes={byte_len} > {LABEL_HARD_BYTES}"
            )

    @pytest.mark.parametrize("axis", [a.value for a in ClarifyAxis])
    def test_labels_recommended_char_limit(self, axis: str):
        for opt in get_options(axis):
            chars = len(opt.label_ko)
            assert chars <= LABEL_RECOMMENDED_CHARS, (
                f"axis={axis} value={opt.value} label='{opt.label_ko}' chars={chars} > {LABEL_RECOMMENDED_CHARS}"
            )

    @pytest.mark.parametrize("axis", [a.value for a in ClarifyAxis])
    def test_labels_no_emoji_or_specials(self, axis: str):
        # R4 — 한글 + 영문 + 공백/하이픈만. 이모지·특수문자 금지.
        for opt in get_options(axis):
            for ch in opt.label_ko:
                code = ord(ch)
                # 한글 음절(가-힣)
                if 0xAC00 <= code <= 0xD7A3:
                    continue
                # 한글 자모
                if 0x3131 <= code <= 0x318E:
                    continue
                # ASCII printable (영문/숫자/공백/하이픈 등)
                if 0x20 <= code <= 0x7E:
                    continue
                pytest.fail(f"axis={axis} value={opt.value} label='{opt.label_ko}' 비허용 문자 U+{code:04X}")

    def test_skip_label_within_limits(self):
        assert len(SKIP_LABEL_KO) <= LABEL_RECOMMENDED_CHARS
        assert len(SKIP_LABEL_KO.encode("utf-8")) <= LABEL_HARD_BYTES


class TestCallbackByteLimit:
    """R3 — clarify:<axis>:<value> 콜백이 Telegram 64바이트 한도 안에 들어감."""

    def test_all_callback_payloads_within_64b(self):
        for axis_val, opts in AXIS_OPTIONS.items():
            for opt in opts:
                payload = f"clarify:{axis_val}:{opt.value}"
                byte_len = len(payload.encode("utf-8"))
                assert byte_len <= CALLBACK_HARD_BYTES, f"{payload} bytes={byte_len} > {CALLBACK_HARD_BYTES}"
            # skip 콜백도 검증.
            payload = f"clarify:{axis_val}:{SKIP_VALUE}"
            byte_len = len(payload.encode("utf-8"))
            assert byte_len <= CALLBACK_HARD_BYTES, f"{payload} bytes={byte_len} > {CALLBACK_HARD_BYTES}"


class TestGetOption:
    def test_known_axis_value_returns_option(self):
        opt = get_option("formality", "casual")
        assert opt is not None
        assert opt.label_ko == "캐주얼"
        assert "casual" in opt.keywords_to_boost

    def test_unknown_axis_returns_none(self):
        assert get_option("nonexistent", "casual") is None

    def test_unknown_value_returns_none(self):
        assert get_option("formality", "nonexistent_value") is None

    def test_skip_returns_none(self):
        # skip 은 항상 None — 호출자가 별도 분기.
        assert get_option("formality", SKIP_VALUE) is None

    def test_each_option_value_unique_per_axis(self):
        for axis_val, opts in AXIS_OPTIONS.items():
            values = [o.value for o in opts]
            assert len(values) == len(set(values)), f"axis={axis_val} 중복 value 감지: {values}"


class TestColorPersonalization:
    """color 축 — user_feature_scores 로 버튼 순서 개인화 + 표시 캡."""

    def test_color_values_map_to_feature_enum(self):
        # value 를 대문자화하면 product_features primary_color / v6 color_family enum 이어야
        # user_feature_scores 조회 키가 정확히 맞는다.
        from app.channels.clarify_values import COLOR_OPTIONS

        enum16 = {
            "BLACK", "WHITE", "GREY", "NAVY", "BLUE", "BEIGE", "BROWN",
            "GREEN", "RED", "PINK", "PURPLE", "ORANGE", "YELLOW", "CREAM", "KHAKI", "MULTI",
        }  # fmt: skip
        for opt in COLOR_OPTIONS:
            assert opt.value.upper() in enum16, opt.value

    def test_cold_user_keeps_static_order_and_caps_at_k(self):
        from app.channels.clarify_values import COLOR_OPTIONS, personalized_color_options

        out = personalized_color_options(None, k=6)
        assert len(out) == 6
        assert [o.value for o in out] == [o.value for o in COLOR_OPTIONS[:6]]

    def test_loved_colors_float_to_top(self):
        from app.channels.clarify_values import personalized_color_options

        # navy/khaki 를 아껴온 유저 → 두 색이 앞으로. (feature_scores 키는 UPPER enum)
        scores = {("color", "NAVY"): 15.0, ("color", "KHAKI"): 8.0}
        out = [o.value for o in personalized_color_options(scores, k=6)]
        assert out[0] == "navy"
        assert out[1] == "khaki"
        assert "black" in out  # 나머지는 기본 순서로 뒤따름

    def test_color_card_is_server_authoritative_and_personalized(self):
        # ask_user_clarification 의 color 분기: LLM options 무시, 서버가 취향 버튼 + 건너뛰기.
        from app.agents.tools.ask_user_clarification import _color_card
        from app.scoring import feature_scores_cache

        feature_scores_cache.clear()
        feature_scores_cache.put("c:42", {("color", "RED"): 20.0})
        prompt, buttons = _color_card({"user_key": "c:42"}, "")
        feature_scores_cache.clear()

        assert prompt  # 서버 기본 프롬프트 채워짐
        assert buttons[0][0]["callback_data"] == "clarify:color:red"  # 취향 1위가 맨 앞
        assert buttons[-1][0]["callback_data"] == "clarify:color:skip"  # 마지막은 건너뛰기
