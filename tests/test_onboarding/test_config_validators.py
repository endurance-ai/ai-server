"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-SEED-002.

Pydantic field validators reject out-of-range values for onboarding settings.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_seed_max_weight_valid_default_passes():
    s = Settings(ONBOARDING_SEED_MAX_WEIGHT=0.7)
    assert s.ONBOARDING_SEED_MAX_WEIGHT == 0.7


def test_seed_max_weight_zero_rejected():
    with pytest.raises(ValidationError):
        Settings(ONBOARDING_SEED_MAX_WEIGHT=0.0)


def test_seed_max_weight_negative_rejected():
    with pytest.raises(ValidationError):
        Settings(ONBOARDING_SEED_MAX_WEIGHT=-0.5)


def test_seed_max_weight_above_one_rejected():
    with pytest.raises(ValidationError):
        Settings(ONBOARDING_SEED_MAX_WEIGHT=1.5)


def test_apify_max_items_valid():
    assert Settings(APIFY_PINTEREST_MAX_ITEMS=50).APIFY_PINTEREST_MAX_ITEMS == 50


def test_apify_max_items_zero_rejected():
    with pytest.raises(ValidationError):
        Settings(APIFY_PINTEREST_MAX_ITEMS=0)


def test_apify_max_items_too_high_rejected():
    with pytest.raises(ValidationError):
        Settings(APIFY_PINTEREST_MAX_ITEMS=500)


def test_apify_concurrency_range():
    assert Settings(APIFY_PINTEREST_CONCURRENCY=5).APIFY_PINTEREST_CONCURRENCY == 5
    with pytest.raises(ValidationError):
        Settings(APIFY_PINTEREST_CONCURRENCY=0)
    with pytest.raises(ValidationError):
        Settings(APIFY_PINTEREST_CONCURRENCY=99)


def test_max_pins_per_turn_range():
    assert Settings(PINTEREST_MAX_PINS_PER_TURN=20).PINTEREST_MAX_PINS_PER_TURN == 20
    with pytest.raises(ValidationError):
        Settings(PINTEREST_MAX_PINS_PER_TURN=0)


def test_negative_ttl_rejected():
    with pytest.raises(ValidationError):
        Settings(PINTEREST_INGEST_CACHE_TTL_S=-1)


def test_negative_rate_limit_rejected():
    with pytest.raises(ValidationError):
        Settings(PINTEREST_CONTINUOUS_RATELIMIT_S=-1)


def test_default_settings_all_valid():
    """A bare `Settings()` constructor (defaults only) must validate."""
    s = Settings()
    assert s.APIFY_PINTEREST_MAX_ITEMS == 80
    assert s.APIFY_PINTEREST_CONCURRENCY == 5
    assert s.ONBOARDING_SEED_MAX_WEIGHT == 0.7
    assert s.PINTEREST_BOOTSTRAP_ENABLED is True
    assert s.ONBOARDING_CARDS_ENABLED is True
    assert s.PINTEREST_INGEST_CACHE_TTL_S == 86400
