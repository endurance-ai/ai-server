"""SPEC-CHAT-STATE-REDIS-001 — unit tests for chat_state helpers.

Covers:
- REQ-CHAT-STATE-001 cursor read/write/TTL.
- REQ-CHAT-STATE-002 impression dedupe membership / clear / TTL.
- REQ-CHAT-STATE-003 fail-open (raising client mock → safe defaults + DEBUG log,
  no WARN/ERROR, no propagate).
- REQ-CHAT-STATE-004 warm_pool tolerates unreachable host without raising.
"""

from __future__ import annotations

import logging

import fakeredis.aioredis
import pytest

from app.infrastructure.cache import chat_state

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def fake_redis(monkeypatch):
    """Per-test fakeredis instance bound to chat_state._pool."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_state, "_pool", client)
    yield client
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        pass
    monkeypatch.setattr(chat_state, "_pool", None)


# --- REQ-CHAT-STATE-001 cursor happy path ---


async def test_set_cursor_then_get_returns_value(fake_redis):
    await chat_state.set_cursor(chat_id=42, n=5)
    assert await chat_state.get_cursor(42) == 5


async def test_get_cursor_unset_returns_zero(fake_redis):
    assert await chat_state.get_cursor(99) == 0


async def test_set_cursor_sets_ttl_24h(fake_redis):
    await chat_state.set_cursor(chat_id=42, n=10)
    ttl = await fake_redis.ttl("kiko:cursor:42")
    assert 0 < ttl <= 86_400


async def test_set_cursor_overwrites_prior_value(fake_redis):
    await chat_state.set_cursor(42, 5)
    await chat_state.set_cursor(42, 15)
    assert await chat_state.get_cursor(42) == 15


# --- REQ-CHAT-STATE-002 impression dedupe happy path ---


async def test_mark_logged_then_is_logged_true(fake_redis):
    await chat_state.mark_logged(42, "prod-A")
    assert await chat_state.is_logged(42, "prod-A") is True
    assert await chat_state.is_logged(42, "prod-B") is False


async def test_clear_logged_drops_set(fake_redis):
    await chat_state.mark_logged(42, "prod-A")
    await chat_state.mark_logged(42, "prod-B")
    await chat_state.clear_logged(42)
    assert await chat_state.is_logged(42, "prod-A") is False
    assert await chat_state.is_logged(42, "prod-B") is False


async def test_mark_logged_sets_ttl_7d(fake_redis):
    await chat_state.mark_logged(42, "prod-A")
    ttl = await fake_redis.ttl("kiko:imp:42")
    assert 0 < ttl <= 604_800


async def test_is_logged_empty_pid_returns_false(fake_redis):
    assert await chat_state.is_logged(42, "") is False
    # mark_logged with empty pid is a no-op (no key created).
    await chat_state.mark_logged(42, "")
    assert await fake_redis.exists("kiko:imp:42") == 0


async def test_different_chats_have_isolated_sets(fake_redis):
    await chat_state.mark_logged(42, "prod-A")
    assert await chat_state.is_logged(42, "prod-A") is True
    assert await chat_state.is_logged(43, "prod-A") is False


# --- REQ-CHAT-STATE-003 fail-open ---


class _RaisingClient:
    """All commands raise — simulates redis down / network error / timeout."""

    async def get(self, *_a, **_k):
        raise ConnectionError("boom")

    async def set(self, *_a, **_k):
        raise ConnectionError("boom")

    async def sismember(self, *_a, **_k):
        raise ConnectionError("boom")

    async def sadd(self, *_a, **_k):
        raise ConnectionError("boom")

    async def expire(self, *_a, **_k):
        raise ConnectionError("boom")

    async def delete(self, *_a, **_k):
        raise ConnectionError("boom")

    async def ping(self, *_a, **_k):
        raise ConnectionError("boom")


@pytest.fixture
def raising_redis(monkeypatch):
    monkeypatch.setattr(chat_state, "_pool", _RaisingClient())
    yield
    monkeypatch.setattr(chat_state, "_pool", None)


async def test_get_cursor_fail_open_returns_zero(raising_redis, caplog):
    caplog.set_level(logging.DEBUG, logger="app.infrastructure.cache.chat_state")
    assert await chat_state.get_cursor(42) == 0
    debug_msgs = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("get_cursor fail-open" in r.message for r in debug_msgs)
    assert all(r.levelname not in ("WARNING", "ERROR") for r in caplog.records)


async def test_set_cursor_fail_open_swallow(raising_redis, caplog):
    caplog.set_level(logging.DEBUG, logger="app.infrastructure.cache.chat_state")
    await chat_state.set_cursor(42, 5)  # no raise
    assert any(r.levelname == "DEBUG" and "set_cursor fail-open" in r.message for r in caplog.records)


async def test_is_logged_fail_open_returns_false(raising_redis):
    assert await chat_state.is_logged(42, "prod-A") is False


async def test_mark_logged_fail_open_swallow(raising_redis):
    # MUST NOT raise.
    await chat_state.mark_logged(42, "prod-A")


async def test_clear_logged_fail_open_swallow(raising_redis):
    await chat_state.clear_logged(42)


async def test_warm_pool_returns_false_on_unreachable(monkeypatch, caplog):
    """warm_pool with an unreachable host returns False — startup proceeds."""
    monkeypatch.setattr(chat_state, "_pool", None)
    monkeypatch.setattr(chat_state.settings, "REDIS_URL", "redis://nonexistent-host-x:6379/0")
    caplog.set_level(logging.INFO, logger="app.infrastructure.cache.chat_state")
    result = await chat_state.warm_pool()
    assert result is False
    # subsequent helper calls also fail-open silently
    assert await chat_state.get_cursor(42) == 0
    # cleanup pool created by warm_pool attempt
    await chat_state.close_pool()


async def test_warm_pool_returns_true_with_fake_redis(monkeypatch):
    """warm_pool's PING succeeds with a healthy fakeredis client."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_state, "_pool", client)
    try:
        assert await chat_state.warm_pool() is True
    finally:
        await client.aclose()
        monkeypatch.setattr(chat_state, "_pool", None)


async def test_close_pool_resets_singleton(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_state, "_pool", client)
    await chat_state.close_pool()
    assert chat_state._pool is None
