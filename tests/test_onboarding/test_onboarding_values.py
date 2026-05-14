"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-CARDS-001 + REQ-ONBOARD-LANG-002.

Catalog shape + uniqueness + label sanity for `app/channels/onboarding_values.py`.
"""

from __future__ import annotations

from app.channels.onboarding_values import (
    COLOR_VALUES,
    FIT_VALUES,
    MOOD_VALUES,
    STAGE_BOUNDS,
    OnboardingOption,
    get_option,
)


def test_mood_has_exactly_8_options():
    assert len(MOOD_VALUES) == 8


def test_color_has_exactly_6_options():
    assert len(COLOR_VALUES) == 6


def test_fit_has_exactly_4_options():
    assert len(FIT_VALUES) == 4


def test_all_options_are_frozen_dataclass_instances():
    for cat in (MOOD_VALUES, COLOR_VALUES, FIT_VALUES):
        for opt in cat:
            assert isinstance(opt, OnboardingOption)


def test_values_unique_per_stage():
    for cat, name in [(MOOD_VALUES, "mood"), (COLOR_VALUES, "color"), (FIT_VALUES, "fit")]:
        values = [o.value for o in cat]
        assert len(values) == len(set(values)), f"duplicate values in {name}: {values}"


def test_labels_non_empty():
    for cat in (MOOD_VALUES, COLOR_VALUES, FIT_VALUES):
        for opt in cat:
            assert opt.ko_label.strip(), f"empty ko_label: {opt!r}"
            assert opt.en_label.strip(), f"empty en_label: {opt!r}"
            assert opt.emoji.strip(), f"empty emoji: {opt!r}"


def test_labels_within_telegram_safe_length():
    for cat in (MOOD_VALUES, COLOR_VALUES, FIT_VALUES):
        for opt in cat:
            assert len(opt.ko_label) <= 16
            assert len(opt.en_label) <= 16


def test_values_are_snake_case_callback_safe():
    """callback_data payload bytes are budget-critical (Telegram 64-byte cap).

    Enforce snake_case alphanum so `onboard:{stage}:toggle:{value}` stays
    parseable and ASCII.
    """
    import re

    pattern = re.compile(r"^[a-z][a-z0-9_]*$")
    for cat in (MOOD_VALUES, COLOR_VALUES, FIT_VALUES):
        for opt in cat:
            assert pattern.match(opt.value), f"non-callback-safe value: {opt.value!r}"


def test_stage_bounds_match_required_spec():
    assert STAGE_BOUNDS["mood"] == (2, 3)
    assert STAGE_BOUNDS["color"] == (2, 3)
    assert STAGE_BOUNDS["fit"] == (1, 2)


def test_get_option_returns_match():
    opt = get_option("mood", "minimal")
    assert opt is not None
    assert opt.value == "minimal"


def test_get_option_returns_none_for_unknown_stage():
    assert get_option("nonsense", "minimal") is None


def test_get_option_returns_none_for_unknown_value():
    assert get_option("mood", "nope_not_real") is None
