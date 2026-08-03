"""Unit tests for click_token (CTR redirect token mint/lookup).

Covers:
- Happy path: mint → lookup returns payload, TTL ≤ 24h.
- Fail-open: raising client → mint returns None, lookup returns None,
  no exception propagates.
- Idempotent multi-tap: lookup_token does NOT delete the key (multiple taps
  resolve to the same payload until TTL expires).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.infrastructure.cache import click_token

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(click_token, "_pool", client)
    yield client
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass
    monkeypatch.setattr(click_token, "_pool", None)


async def test_mint_then_lookup_roundtrips(fake_redis):
    token = await click_token.mint_token(
        chat_id=42, rec_id="trace-abc", product_id="pid-9", product_url="https://shop.example/p/9"
    )
    assert token and isinstance(token, str)
    payload = await click_token.lookup_token(token)
    assert payload == {
        "chat_id": 42,
        "rec_id": "trace-abc",
        "product_id": "pid-9",
        "product_url": "https://shop.example/p/9",
    }


async def test_mint_sets_ttl_24h(fake_redis):
    token = await click_token.mint_token(1, None, "p", "https://x")
    assert token
    ttl = await fake_redis.ttl(f"kiko:click:{token}")
    assert 0 < ttl <= 86_400


async def test_lookup_miss_returns_none(fake_redis):
    assert await click_token.lookup_token("does-not-exist") is None


async def test_lookup_empty_token_returns_none(fake_redis):
    assert await click_token.lookup_token("") is None


async def test_lookup_is_non_destructive(fake_redis):
    """Multi-tap safety — same token resolves twice."""
    token = await click_token.mint_token(1, "t", "p", "https://x")
    assert await click_token.lookup_token(token) is not None
    assert await click_token.lookup_token(token) is not None


async def test_mint_fail_open_when_setex_raises(monkeypatch):
    """Redis-down → mint returns None, never raises."""

    class _Raising:
        async def setex(self, *_a, **_kw):
            raise RuntimeError("redis down")

    monkeypatch.setattr(click_token, "_pool", _Raising())
    token = await click_token.mint_token(1, "t", "p", "https://x")
    assert token is None


async def test_lookup_fail_open_when_get_raises(monkeypatch):
    class _Raising:
        async def get(self, *_a, **_kw):
            raise RuntimeError("redis down")

    monkeypatch.setattr(click_token, "_pool", _Raising())
    assert await click_token.lookup_token("any") is None


async def test_lookup_handles_garbage_value(fake_redis):
    """Non-JSON value in Redis (catastrophic race) → return None."""
    await fake_redis.set("kiko:click:bad-token", "not json {{{")
    assert await click_token.lookup_token("bad-token") is None
