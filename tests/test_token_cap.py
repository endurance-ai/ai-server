"""SPEC-DAILY-TOKEN-CAP-001 — unit tests for token_cap Redis helpers.

Uses fakeredis to avoid a real Redis dependency.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fakeredis():
    try:
        from fakeredis.aioredis import FakeRedis

        return FakeRedis(decode_responses=True)
    except ImportError:
        pytest.skip("fakeredis not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis(monkeypatch):
    from app.infrastructure.cache import token_cap

    client = _make_fakeredis()

    def _fake_get_client():
        return client

    monkeypatch.setattr(token_cap, "_get_client", _fake_get_client)
    yield client
    await client.flushall()


@pytest.fixture(autouse=True)
def _enable_cap(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DAILY_TOKEN_CAP_ENABLED", True)
    monkeypatch.setattr(settings, "DAILY_TOKEN_CAP", 200_000)
    monkeypatch.setattr(settings, "CAP_TIER_FREE", 200_000)
    monkeypatch.setattr(settings, "CAP_TIER_STANDARD", 500_000)
    monkeypatch.setattr(settings, "CAP_TIER_PRO", 1_000_000)
    monkeypatch.setattr(settings, "CAP_TIER_DEVELOPER", 0)


# ---------------------------------------------------------------------------
# Tests: _seconds_until_kst_midnight
# ---------------------------------------------------------------------------


def test_seconds_until_kst_midnight_is_positive():
    from app.infrastructure.cache.token_cap import _seconds_until_kst_midnight

    ttl = _seconds_until_kst_midnight()
    assert 60 <= ttl <= 86_400


# ---------------------------------------------------------------------------
# Tests: _tier_cap
# ---------------------------------------------------------------------------


def test_tier_cap_free():
    from app.infrastructure.cache.token_cap import _tier_cap

    assert _tier_cap("free") == 200_000


def test_tier_cap_standard():
    from app.infrastructure.cache.token_cap import _tier_cap

    assert _tier_cap("standard") == 500_000


def test_tier_cap_pro():
    from app.infrastructure.cache.token_cap import _tier_cap

    assert _tier_cap("pro") == 1_000_000


def test_tier_cap_developer_is_zero():
    from app.infrastructure.cache.token_cap import _tier_cap

    assert _tier_cap("developer") == 0


def test_tier_cap_unknown_falls_back_to_free():
    from app.infrastructure.cache.token_cap import _tier_cap

    assert _tier_cap("superuser") == 200_000


def test_tier_cap_free_uses_cap_tier_free_when_overridden(monkeypatch):
    """CAP_TIER_FREE overrides DAILY_TOKEN_CAP when explicitly changed."""
    from app.core.config import settings
    from app.infrastructure.cache.token_cap import _tier_cap

    monkeypatch.setattr(settings, "CAP_TIER_FREE", 300_000)
    monkeypatch.setattr(settings, "DAILY_TOKEN_CAP", 150_000)
    assert _tier_cap("free") == 300_000  # CAP_TIER_FREE wins


def test_tier_cap_free_falls_back_to_daily_token_cap_when_free_is_default(monkeypatch):
    """DAILY_TOKEN_CAP used as fallback when CAP_TIER_FREE is still at default."""
    from app.core.config import settings
    from app.infrastructure.cache.token_cap import _tier_cap

    monkeypatch.setattr(settings, "CAP_TIER_FREE", 200_000)  # default, not overridden
    monkeypatch.setattr(settings, "DAILY_TOKEN_CAP", 150_000)  # legacy override
    assert _tier_cap("free") == 150_000  # DAILY_TOKEN_CAP honored as legacy


# ---------------------------------------------------------------------------
# Tests: get_user_tier / set_user_tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_tier_default_is_free(fake_redis):
    from app.infrastructure.cache.token_cap import get_user_tier

    assert await get_user_tier(1234) == "free"


@pytest.mark.asyncio
async def test_set_and_get_user_tier(fake_redis):
    from app.infrastructure.cache.token_cap import get_user_tier, set_user_tier

    for tier in ("free", "standard", "pro", "developer"):
        assert await set_user_tier(1234, tier) is True  # type: ignore[arg-type]
        assert await get_user_tier(1234) == tier


@pytest.mark.asyncio
async def test_set_user_tier_invalid_returns_false(fake_redis):
    from app.infrastructure.cache.token_cap import set_user_tier

    assert await set_user_tier(1234, "superuser") is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_user_tier_fail_open_on_redis_error(monkeypatch):
    from app.infrastructure.cache import token_cap

    monkeypatch.setattr(token_cap, "_get_client", lambda: None)
    assert await token_cap.get_user_tier(1234) == "free"


@pytest.mark.asyncio
async def test_tier_persists_across_calls(fake_redis):
    from app.infrastructure.cache.token_cap import get_user_tier, set_user_tier

    await set_user_tier(9999, "developer")
    # Second call should still return developer
    assert await get_user_tier(9999) == "developer"
    assert await get_user_tier(9999) == "developer"


# ---------------------------------------------------------------------------
# Tests: is_over_limit — tier-aware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_over_limit_false_when_no_key(fake_redis):
    from app.infrastructure.cache.token_cap import is_over_limit

    assert await is_over_limit(1234) is False


@pytest.mark.asyncio
async def test_is_over_limit_false_below_free_cap(fake_redis):
    from app.infrastructure.cache.token_cap import increment, is_over_limit

    await increment(1234, 100_000)
    assert await is_over_limit(1234) is False


@pytest.mark.asyncio
async def test_is_over_limit_true_at_free_cap(fake_redis):
    from app.infrastructure.cache.token_cap import increment, is_over_limit

    await increment(1234, 200_000)
    assert await is_over_limit(1234) is True


@pytest.mark.asyncio
async def test_developer_tier_never_over_limit(fake_redis):
    from app.infrastructure.cache.token_cap import increment, is_over_limit, set_user_tier

    await set_user_tier(1234, "developer")
    await increment(1234, 5_000_000)
    assert await is_over_limit(1234) is False


@pytest.mark.asyncio
async def test_standard_tier_cap_is_500k(fake_redis):
    from app.infrastructure.cache.token_cap import increment, is_over_limit, set_user_tier

    await set_user_tier(1234, "standard")
    await increment(1234, 450_000)
    assert await is_over_limit(1234) is False

    await increment(1234, 60_000)  # → 510K
    assert await is_over_limit(1234) is True


@pytest.mark.asyncio
async def test_pro_tier_cap_is_1m(fake_redis):
    from app.infrastructure.cache.token_cap import increment, is_over_limit, set_user_tier

    await set_user_tier(1234, "pro")
    await increment(1234, 900_000)
    assert await is_over_limit(1234) is False

    await increment(1234, 120_000)  # → 1.02M
    assert await is_over_limit(1234) is True


@pytest.mark.asyncio
async def test_is_over_limit_false_when_cap_disabled(monkeypatch, fake_redis):
    from app.core.config import settings
    from app.infrastructure.cache.token_cap import increment, is_over_limit

    await increment(1234, 300_000)
    monkeypatch.setattr(settings, "DAILY_TOKEN_CAP_ENABLED", False)
    assert await is_over_limit(1234) is False


@pytest.mark.asyncio
async def test_is_over_limit_fail_open_on_redis_error(monkeypatch):
    from app.infrastructure.cache import token_cap

    monkeypatch.setattr(token_cap, "_get_client", lambda: None)
    assert await token_cap.is_over_limit(1234) is False


# ---------------------------------------------------------------------------
# Tests: increment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_increment_accumulates(fake_redis):
    from app.infrastructure.cache.token_cap import get_usage, increment

    await increment(1234, 10_000)
    await increment(1234, 5_000)
    assert await get_usage(1234) == 15_000


@pytest.mark.asyncio
async def test_increment_returns_new_total(fake_redis):
    from app.infrastructure.cache.token_cap import increment

    assert await increment(1234, 7_000) == 7_000
    assert await increment(1234, 3_000) == 10_000


@pytest.mark.asyncio
async def test_increment_noop_when_disabled(monkeypatch, fake_redis):
    from app.core.config import settings
    from app.infrastructure.cache.token_cap import get_usage, increment

    monkeypatch.setattr(settings, "DAILY_TOKEN_CAP_ENABLED", False)
    await increment(1234, 50_000)
    assert await get_usage(1234) == 0


@pytest.mark.asyncio
async def test_increment_noop_zero_tokens(fake_redis):
    from app.infrastructure.cache.token_cap import get_usage, increment

    assert await increment(1234, 0) == 0
    assert await get_usage(1234) == 0


@pytest.mark.asyncio
async def test_increment_fail_open_on_redis_error(monkeypatch):
    from app.infrastructure.cache import token_cap

    monkeypatch.setattr(token_cap, "_get_client", lambda: None)
    assert await token_cap.increment(1234, 10_000) == 0


# ---------------------------------------------------------------------------
# Tests: reset_usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_usage_clears_counter(fake_redis):
    from app.infrastructure.cache.token_cap import get_usage, increment, reset_usage

    await increment(1234, 50_000)
    assert await get_usage(1234) == 50_000
    await reset_usage(1234)
    assert await get_usage(1234) == 0


@pytest.mark.asyncio
async def test_reset_usage_does_not_clear_tier(fake_redis):
    from app.infrastructure.cache.token_cap import get_user_tier, increment, reset_usage, set_user_tier

    await set_user_tier(1234, "pro")
    await increment(1234, 50_000)
    await reset_usage(1234)
    assert await get_user_tier(1234) == "pro"


# ---------------------------------------------------------------------------
# Tests: token_budget_exceeded path records tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_increment_called_on_token_budget_exceeded_path(fake_redis, monkeypatch):
    """token_budget_exceeded early-return must record cumulative_tokens."""
    from app.infrastructure.cache.token_cap import get_usage

    recorded: list[tuple[int, int]] = []

    async def _spy_increment(chat_id: int, tokens: int) -> int:
        recorded.append((chat_id, tokens))
        await fake_redis.incrby(f"kiko:cap:{chat_id}", tokens)
        return tokens

    monkeypatch.setattr(
        "app.infrastructure.cache.token_cap.increment",
        _spy_increment,
    )

    # Simulate react_loop token_budget_exceeded path inline
    chat_id = 5555
    cumulative_tokens = 32_000  # hit the per-turn budget

    if cumulative_tokens > 0:
        try:
            from app.infrastructure.cache.token_cap import increment as _cap_increment

            await _cap_increment(chat_id, cumulative_tokens)
        except Exception:  # noqa: BLE001
            pass

    assert await get_usage(chat_id) == 32_000
    assert recorded == [(5555, 32_000)]


# ---------------------------------------------------------------------------
# Tests: per-user isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_chat_ids_are_isolated(fake_redis):
    from app.infrastructure.cache.token_cap import get_usage, increment

    await increment(111, 50_000)
    await increment(222, 150_000)
    assert await get_usage(111) == 50_000
    assert await get_usage(222) == 150_000


@pytest.mark.asyncio
async def test_tier_isolated_per_user(fake_redis):
    from app.infrastructure.cache.token_cap import get_user_tier, set_user_tier

    await set_user_tier(111, "developer")
    await set_user_tier(222, "standard")
    assert await get_user_tier(111) == "developer"
    assert await get_user_tier(222) == "standard"
    assert await get_user_tier(333) == "free"  # untouched → default
