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
    """Return a fakeredis.aioredis.FakeRedis instance, skip if unavailable."""
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
    """Monkeypatch the token_cap module to use fakeredis."""
    from app.infrastructure.cache import token_cap

    client = _make_fakeredis()

    def _fake_get_client():
        return client

    monkeypatch.setattr(token_cap, "_get_client", _fake_get_client)
    yield client
    await client.flushall()


@pytest.fixture(autouse=True)
def _enable_cap(monkeypatch):
    """Enable the daily token cap for all tests in this module."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "DAILY_TOKEN_CAP_ENABLED", True)
    monkeypatch.setattr(settings, "DAILY_TOKEN_CAP", 200_000)


# ---------------------------------------------------------------------------
# Tests: _seconds_until_kst_midnight
# ---------------------------------------------------------------------------


def test_seconds_until_kst_midnight_is_positive():
    from app.infrastructure.cache.token_cap import _seconds_until_kst_midnight

    ttl = _seconds_until_kst_midnight()
    assert 60 <= ttl <= 86_400


# ---------------------------------------------------------------------------
# Tests: is_over_limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_over_limit_false_when_no_key(fake_redis):
    from app.infrastructure.cache.token_cap import is_over_limit

    assert await is_over_limit(1234) is False


@pytest.mark.asyncio
async def test_is_over_limit_false_below_cap(fake_redis):
    from app.infrastructure.cache.token_cap import increment, is_over_limit

    await increment(1234, 100_000)
    assert await is_over_limit(1234) is False


@pytest.mark.asyncio
async def test_is_over_limit_true_at_cap(fake_redis):
    from app.infrastructure.cache.token_cap import increment, is_over_limit

    await increment(1234, 200_000)
    assert await is_over_limit(1234) is True


@pytest.mark.asyncio
async def test_is_over_limit_true_above_cap(fake_redis):
    from app.infrastructure.cache.token_cap import increment, is_over_limit

    await increment(1234, 250_000)
    assert await is_over_limit(1234) is True


@pytest.mark.asyncio
async def test_is_over_limit_fail_open_when_disabled(monkeypatch, fake_redis):
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

    total = await increment(1234, 7_000)
    assert total == 7_000
    total2 = await increment(1234, 3_000)
    assert total2 == 10_000


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

    result = await increment(1234, 0)
    assert result == 0
    assert await get_usage(1234) == 0


@pytest.mark.asyncio
async def test_increment_fail_open_on_redis_error(monkeypatch):
    from app.infrastructure.cache import token_cap

    monkeypatch.setattr(token_cap, "_get_client", lambda: None)
    result = await token_cap.increment(1234, 10_000)
    assert result == 0


# ---------------------------------------------------------------------------
# Tests: get_usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_usage_zero_when_no_key(fake_redis):
    from app.infrastructure.cache.token_cap import get_usage

    assert await get_usage(9999) == 0


@pytest.mark.asyncio
async def test_get_usage_after_increment(fake_redis):
    from app.infrastructure.cache.token_cap import get_usage, increment

    await increment(9999, 12_000)
    assert await get_usage(9999) == 12_000


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
