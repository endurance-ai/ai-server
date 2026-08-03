"""Unit tests for /r/{token} outbound redirect endpoint.

Covers:
- Happy path: valid token → 302 to product_url + emit fired.
- Miss / expired: 410 Gone (friendly text).
- Garbage payload (no product_url): 410 Gone.
- Endpoint never raises even when lookup or emit fail.
"""

from __future__ import annotations

from unittest.mock import patch

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app.infrastructure.cache import click_token
from app.main import app

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


async def test_redirect_happy_path(fake_redis):
    token = await click_token.mint_token(42, "trace-x", "pid-1", "https://shop.example/p/1")
    assert token

    with patch("app.api.redirect.emit") as mock_emit:
        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/r/{token}")

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://shop.example/p/1"
    # `card_clicked` event emitted with rec_id bound to langfuse_trace
    mock_emit.assert_called_once()
    kwargs = mock_emit.call_args.kwargs
    assert kwargs["event_type"] == "card_clicked"
    assert kwargs["langfuse_trace"] == "trace-x"
    assert kwargs["payload"]["product_id"] == "pid-1"


async def test_redirect_miss_returns_410(fake_redis):
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/r/does-not-exist")
    assert resp.status_code == 410


async def test_redirect_handles_missing_product_url(fake_redis):
    """Token resolves but payload has no product_url → 410, never 5xx."""
    import json

    await fake_redis.set("kiko:click:no-url", json.dumps({"chat_id": 1}))
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/r/no-url")
    assert resp.status_code == 410


async def test_redirect_emit_failure_does_not_break_redirect(fake_redis):
    """If emit() raises, user STILL gets the 302 (observability is best-effort)."""
    token = await click_token.mint_token(1, "t", "p", "https://example.com/x")

    with patch("app.api.redirect.emit", side_effect=RuntimeError("log down")):
        client = TestClient(app, follow_redirects=False)
        resp = client.get(f"/r/{token}")

    assert resp.status_code == 302
    assert resp.headers["location"] == "https://example.com/x"
