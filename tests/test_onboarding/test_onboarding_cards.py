"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-CARDS-001, REQ-ONBOARD-CARDS-002.

Card builders + strict callback parser tests.

Covers:
  - 5 builder snapshots (text + button matrix + callback shape) — KO/EN parity
  - parse_onboard_callback — valid matrix + 5 malformed cases
"""

from __future__ import annotations

import pytest

from app.channels.onboarding_cards import (
    OnboardCallback,
    build_color_card,
    build_fit_card,
    build_mood_card,
    build_pinterest_card,
    build_restart_confirmation_card,
    parse_onboard_callback,
)
from app.channels.onboarding_values import COLOR_VALUES, FIT_VALUES, MOOD_VALUES


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────
def _flatten_callbacks(kb: list[list[tuple[str, str]]]) -> list[str]:
    return [cb for row in kb for _label, cb in row]


def _flatten_labels(kb: list[list[tuple[str, str]]]) -> list[str]:
    return [label for row in kb for label, _cb in row]


# ────────────────────────────────────────────────────────────────────────────
# Multi-select cards — mood / color / fit
# ────────────────────────────────────────────────────────────────────────────
class TestMoodCard:
    def test_ko_text_and_button_count(self):
        text, kb = build_mood_card("ko", [])
        assert "무드" in text
        # 8 options + 1 footer row (Next + Skip)
        callbacks = _flatten_callbacks(kb)
        assert len(callbacks) == 8 + 2

    def test_en_text(self):
        text, _kb = build_mood_card("en", [])
        assert "mood" in text.lower()

    def test_all_callback_shapes_valid(self):
        _text, kb = build_mood_card("ko", [])
        callbacks = _flatten_callbacks(kb)
        # 8 toggles
        toggle_cbs = [c for c in callbacks if c.startswith("onboard:mood:toggle:")]
        assert len(toggle_cbs) == 8
        # done + skip
        assert "onboard:mood:done" in callbacks
        assert "onboard:mood:skip" in callbacks

    def test_callback_bytes_under_32(self):
        _text, kb = build_mood_card("ko", [])
        for cb in _flatten_callbacks(kb):
            assert len(cb.encode()) <= 32, cb

    def test_selection_marker_shown(self):
        _text, kb = build_mood_card("ko", ["minimal"])
        labels = _flatten_labels(kb)
        marked = [lbl for lbl in labels if lbl.startswith("✓ ")]
        assert len(marked) == 1
        assert "미니멀" in marked[0]

    def test_unknown_selection_ignored(self):
        # Stale persisted state with bogus value should NOT crash render.
        _text, kb = build_mood_card("ko", ["nonexistent_value"])
        marked = [lbl for lbl in _flatten_labels(kb) if lbl.startswith("✓ ")]
        assert marked == []

    def test_callback_values_match_catalog(self):
        _text, kb = build_mood_card("en", [])
        toggle_values = [cb.split(":")[-1] for cb in _flatten_callbacks(kb) if "toggle" in cb]
        catalog_values = [o.value for o in MOOD_VALUES]
        assert sorted(toggle_values) == sorted(catalog_values)


class TestColorCard:
    def test_button_count_six_options(self):
        _text, kb = build_color_card("ko", [])
        toggles = [c for c in _flatten_callbacks(kb) if "toggle" in c]
        assert len(toggles) == 6

    def test_en_done_label(self):
        _text, kb = build_color_card("en", [])
        labels = _flatten_labels(kb)
        assert any("Next" in lbl for lbl in labels)

    def test_callback_values_match_catalog(self):
        _text, kb = build_color_card("ko", ["mono", "pastel"])
        toggle_values = [cb.split(":")[-1] for cb in _flatten_callbacks(kb) if "toggle" in cb]
        catalog_values = [o.value for o in COLOR_VALUES]
        assert sorted(toggle_values) == sorted(catalog_values)

    def test_two_marked(self):
        _text, kb = build_color_card("ko", ["mono", "pastel"])
        marked = [lbl for lbl in _flatten_labels(kb) if lbl.startswith("✓ ")]
        assert len(marked) == 2


class TestFitCard:
    def test_four_options(self):
        _text, kb = build_fit_card("ko", [])
        toggles = [c for c in _flatten_callbacks(kb) if "toggle" in c]
        assert len(toggles) == 4

    def test_callback_values_match_catalog(self):
        _text, kb = build_fit_card("en", [])
        toggle_values = [cb.split(":")[-1] for cb in _flatten_callbacks(kb) if "toggle" in cb]
        catalog_values = [o.value for o in FIT_VALUES]
        assert sorted(toggle_values) == sorted(catalog_values)


# ────────────────────────────────────────────────────────────────────────────
# Pinterest + Restart cards (single-row terminal cards)
# ────────────────────────────────────────────────────────────────────────────
class TestPinterestCard:
    def test_ko_text_mentions_pinterest(self):
        text, _kb = build_pinterest_card("ko")
        assert "핀터레스트" in text

    def test_en_text_mentions_pinterest(self):
        text, _kb = build_pinterest_card("en")
        assert "pinterest" in text.lower()

    def test_buttons_url_mode_and_skip(self):
        _text, kb = build_pinterest_card("ko")
        cbs = _flatten_callbacks(kb)
        assert "onboard:pinterest:url_mode" in cbs
        assert "onboard:pinterest:skip" in cbs

    def test_single_row(self):
        _text, kb = build_pinterest_card("en")
        assert len(kb) == 1
        assert len(kb[0]) == 2


class TestRestartCard:
    def test_ko_text(self):
        text, _kb = build_restart_confirmation_card("ko")
        assert "다시" in text

    def test_en_text(self):
        text, _kb = build_restart_confirmation_card("en")
        assert "again" in text.lower()

    def test_yes_no_callbacks(self):
        _text, kb = build_restart_confirmation_card("ko")
        cbs = _flatten_callbacks(kb)
        assert "onboard:restart:yes" in cbs
        assert "onboard:restart:no" in cbs


# ────────────────────────────────────────────────────────────────────────────
# KO/EN parity — every card returns content in both languages
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "builder",
    [
        build_mood_card,
        build_color_card,
        build_fit_card,
    ],
)
def test_ko_en_parity_same_button_count(builder):
    _text_ko, kb_ko = builder("ko", [])
    _text_en, kb_en = builder("en", [])
    ko_cbs = _flatten_callbacks(kb_ko)
    en_cbs = _flatten_callbacks(kb_en)
    # Callback data is language-independent (snake_case identifiers).
    assert ko_cbs == en_cbs
    # Same button shape.
    assert len(_flatten_labels(kb_ko)) == len(_flatten_labels(kb_en))


# ────────────────────────────────────────────────────────────────────────────
# parse_onboard_callback — strict parser matrix
# ────────────────────────────────────────────────────────────────────────────
class TestParseOnboardCallback:
    @pytest.mark.parametrize(
        ("cb", "expected"),
        [
            (
                "onboard:mood:toggle:minimal",
                OnboardCallback(stage="mood", action="toggle", value="minimal", raw="onboard:mood:toggle:minimal"),
            ),
            (
                "onboard:color:toggle:pastel",
                OnboardCallback(stage="color", action="toggle", value="pastel", raw="onboard:color:toggle:pastel"),
            ),
            (
                "onboard:fit:toggle:oversize",
                OnboardCallback(stage="fit", action="toggle", value="oversize", raw="onboard:fit:toggle:oversize"),
            ),
            (
                "onboard:mood:done",
                OnboardCallback(stage="mood", action="done", value=None, raw="onboard:mood:done"),
            ),
            (
                "onboard:mood:skip",
                OnboardCallback(stage="mood", action="skip", value=None, raw="onboard:mood:skip"),
            ),
            (
                "onboard:pinterest:skip",
                OnboardCallback(stage="pinterest", action="skip", value=None, raw="onboard:pinterest:skip"),
            ),
            (
                "onboard:pinterest:url_mode",
                OnboardCallback(stage="pinterest", action="url_mode", value=None, raw="onboard:pinterest:url_mode"),
            ),
            (
                "onboard:restart:yes",
                OnboardCallback(stage="restart", action="yes", value=None, raw="onboard:restart:yes"),
            ),
            (
                "onboard:restart:no",
                OnboardCallback(stage="restart", action="no", value=None, raw="onboard:restart:no"),
            ),
        ],
    )
    def test_valid_callbacks(self, cb, expected):
        assert parse_onboard_callback(cb) == expected

    @pytest.mark.parametrize(
        "cb",
        [
            "",  # empty
            None,  # None
            "clarify:mood:toggle:minimal",  # wrong prefix
            "onboard:mood",  # too few segments
            "onboard:mood:toggle:minimal:extra",  # too many segments
            "onboard:unknown:toggle:x",  # unknown stage
            "onboard:mood:explode",  # unknown action
            "onboard:mood:toggle",  # toggle without value
            "onboard:mood:toggle:bogus_value",  # unknown option value
            "onboard:mood:done:extra",  # non-toggle action with value
            "onboard:pinterest:toggle:foo",  # toggle action not allowed on pinterest stage
            "onboard:restart:toggle:yes",  # toggle on restart stage
        ],
    )
    def test_malformed_returns_none(self, cb):
        assert parse_onboard_callback(cb) is None

    def test_case_insensitive_action_and_stage(self):
        assert parse_onboard_callback("onboard:MOOD:Toggle:minimal") is not None

    def test_value_lowercased(self):
        out = parse_onboard_callback("onboard:mood:toggle:Minimal")
        # Catalog stores lowercase 'minimal' — case normalization succeeds.
        assert out is not None
        assert out.value == "minimal"
