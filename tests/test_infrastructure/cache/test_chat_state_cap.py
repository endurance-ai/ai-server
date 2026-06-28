"""SPEC-CHAT-STATE-REDIS-001 — cap-box helpers + fail-open paths.

Complements test_chat_state.py (cursor / impression happy path) by covering:
- the cap_shown / cap_silenced / cap_seen helper happy paths,
- the client-None fail-open branch (every helper returns its safe default),
- the raising-client fail-open branch,
- warm_pool / close_pool lifecycle.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.infrastructure.cache import chat_state

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_state, "_pool", client)
    yield client
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass
    monkeypatch.setattr(chat_state, "_pool", None)


class _RaisingClient:
    """Every awaited method raises — exercises the fail-open except branches."""

    def __getattr__(self, _name):
        async def _boom(*args, **kwargs):
            raise RuntimeError("redis down")

        return _boom


@pytest.fixture
def raising_client(monkeypatch):
    monkeypatch.setattr(chat_state, "_get_client", lambda: _RaisingClient())


@pytest.fixture
def no_client(monkeypatch):
    monkeypatch.setattr(chat_state, "_get_client", lambda: None)


# ── cap-box happy paths ─────────────────────────────────────────────────────


async def test_cap_shown_roundtrip(fake_redis):
    assert await chat_state.is_cap_shown(5) is False
    await chat_state.mark_cap_shown(5)
    assert await chat_state.is_cap_shown(5) is True
    ttl = await fake_redis.ttl("kiko:cap_shown:5")
    assert 0 < ttl <= 21_600


async def test_cap_silenced_roundtrip(fake_redis):
    assert await chat_state.is_cap_silenced(5) is False
    await chat_state.mark_cap_silenced(5)
    assert await chat_state.is_cap_silenced(5) is True


async def test_cap_seen_roundtrip_and_clear(fake_redis):
    await chat_state.mark_cap_seen(5)
    assert await chat_state.is_cap_seen(5) is True
    await chat_state.clear_cap_seen(5)
    assert await chat_state.is_cap_seen(5) is False


# ── client-None fail-open (safe defaults, no raise) ─────────────────────────


async def test_all_helpers_safe_default_when_no_client(no_client):
    assert await chat_state.get_cursor(1) == 0
    await chat_state.set_cursor(1, 5)  # no-op
    assert await chat_state.is_logged(1, "p") is False
    await chat_state.mark_logged(1, "p")  # no-op
    await chat_state.clear_logged(1)  # no-op
    assert await chat_state.is_cap_shown(1) is False
    await chat_state.mark_cap_shown(1)
    assert await chat_state.is_cap_silenced(1) is False
    await chat_state.mark_cap_silenced(1)
    assert await chat_state.is_cap_seen(1) is False
    await chat_state.mark_cap_seen(1)
    await chat_state.clear_cap_seen(1)


# ── raising-client fail-open (swallow, safe defaults) ───────────────────────


async def test_all_helpers_fail_open_when_client_raises(raising_client):
    assert await chat_state.get_cursor(1) == 0
    await chat_state.set_cursor(1, 5)
    assert await chat_state.is_logged(1, "p") is False
    await chat_state.mark_logged(1, "p")
    await chat_state.clear_logged(1)
    assert await chat_state.is_cap_shown(1) is False
    await chat_state.mark_cap_shown(1)
    assert await chat_state.is_cap_silenced(1) is False
    await chat_state.mark_cap_silenced(1)
    assert await chat_state.is_cap_seen(1) is False
    await chat_state.mark_cap_seen(1)
    await chat_state.clear_cap_seen(1)


async def test_empty_product_id_short_circuits(fake_redis):
    # falsy product_id is never deduped and never creates a key.
    assert await chat_state.is_logged(1, "") is False
    await chat_state.mark_logged(1, "")
    assert await fake_redis.exists("kiko:imp:1") == 0


async def test_get_cursor_unparseable_value_fails_open(fake_redis):
    await fake_redis.set("kiko:cursor:9", "not-an-int")
    assert await chat_state.get_cursor(9) == 0


# ── lifecycle ───────────────────────────────────────────────────────────────


async def test_warm_pool_success(fake_redis):
    assert await chat_state.warm_pool() is True


async def test_warm_pool_returns_false_when_no_client(no_client):
    assert await chat_state.warm_pool() is False


async def test_warm_pool_returns_false_when_ping_raises(raising_client):
    assert await chat_state.warm_pool() is False


async def test_close_pool_resets_singleton(fake_redis):
    await chat_state.close_pool()
    assert chat_state._pool is None


async def test_close_pool_noop_when_already_none(monkeypatch):
    monkeypatch.setattr(chat_state, "_pool", None)
    await chat_state.close_pool()  # must not raise
    assert chat_state._pool is None


# ── _mask_url (async wrapper to stay under the module-level asyncio mark) ────


async def test_mask_url_variants():
    assert chat_state._mask_url("redis://:secret@host:6379/1") == "redis://****@host:6379/1"
    assert chat_state._mask_url("redis://host:6379/1") == "redis://host:6379/1"
