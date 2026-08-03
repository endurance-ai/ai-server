"""Apify-backed Instagram resolver — unit tests.

Covers: token-missing fallback, single Image, Sidecar carousel, Reel reject,
402 circuit-breaker open/recover, 401/403/408/network failure, malformed
payloads, and the link_resolver wiring.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.channels import instagram_apify as ia
from app.channels import link_resolver as lr


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Fresh circuit + fresh link_resolver cache for every test."""
    ia._reset_circuit_for_tests()
    lr._CACHE.clear()
    # Predictable settings.
    monkeypatch.setattr("app.core.config.settings.APIFY_TOKEN", "test-token", raising=False)
    monkeypatch.setattr("app.core.config.settings.APIFY_402_COOLOFF_S", 60, raising=False)
    yield
    ia._reset_circuit_for_tests()
    lr._CACHE.clear()


def _resp(status: int, json_body: Any = None, text: str = "") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=json_body if json_body is not None else None,
        text=text if json_body is None else None,
        request=httpx.Request("POST", "https://api.apify.com/v2/x"),
    )


def _patch_post(response: httpx.Response | Exception):
    """Patch httpx.AsyncClient.post to return `response` (or raise if Exception)."""

    async def _fake_post(self, url, **kwargs):  # noqa: ARG001
        if isinstance(response, Exception):
            raise response
        return response

    return patch.object(httpx.AsyncClient, "post", _fake_post)


# ─── token / circuit ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_token_returns_empty(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.APIFY_TOKEN", "", raising=False)
    assert await ia.fetch_post_images("https://instagram.com/p/AAA/") == []


@pytest.mark.asyncio
async def test_402_opens_circuit_and_blocks_subsequent_calls():
    body = [{"type": "Image", "displayUrl": "https://cdn/x.jpg", "shortCode": "AAA"}]
    with _patch_post(_resp(402, text="payment required")):
        first = await ia.fetch_post_images("https://instagram.com/p/AAA/")
    assert first == []
    # Circuit now OPEN — even a would-be-successful response is short-circuited.
    with _patch_post(_resp(200, json_body=body)):
        second = await ia.fetch_post_images("https://instagram.com/p/BBB/")
    assert second == []


@pytest.mark.asyncio
async def test_402_circuit_recovers_after_cooloff(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.APIFY_402_COOLOFF_S", 60, raising=False)
    with _patch_post(_resp(402)):
        await ia.fetch_post_images("https://instagram.com/p/AAA/")
    # Fast-forward past cool-off.
    ia._CIRCUIT_OPEN_UNTIL = time.monotonic() - 1
    body = [{"type": "Image", "displayUrl": "https://cdn/x.jpg", "shortCode": "AAA"}]
    with _patch_post(_resp(200, json_body=body)):
        out = await ia.fetch_post_images("https://instagram.com/p/AAA/")
    assert out == ["https://cdn/x.jpg"]


# ─── parsing ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_image_post():
    body = [
        {
            "type": "Image",
            "displayUrl": "https://cdn.instagram.com/p1.jpg",
            "shortCode": "AAA",
            "ownerUsername": "alice",
        }
    ]
    with _patch_post(_resp(200, json_body=body)):
        out = await ia.fetch_post_images("https://instagram.com/p/AAA/")
    assert out == ["https://cdn.instagram.com/p1.jpg"]


@pytest.mark.asyncio
async def test_sidecar_returns_all_slides_in_order():
    body = [
        {
            "type": "Sidecar",
            "shortCode": "AAA",
            "displayUrl": "https://cdn/cover.jpg",
            "childPosts": [
                {"displayUrl": "https://cdn/s1.jpg"},
                {"displayUrl": "https://cdn/s2.jpg"},
                {"displayUrl": "https://cdn/s3.jpg"},
            ],
        }
    ]
    with _patch_post(_resp(200, json_body=body)):
        out = await ia.fetch_post_images("https://instagram.com/p/AAA/")
    assert out == [
        "https://cdn/s1.jpg",
        "https://cdn/s2.jpg",
        "https://cdn/s3.jpg",
    ]


@pytest.mark.asyncio
async def test_reel_returns_empty():
    body = [
        {
            "type": "Video",
            "productType": "clips",
            "displayUrl": "https://cdn/poster.jpg",
            "shortCode": "AAA",
        }
    ]
    with _patch_post(_resp(200, json_body=body)):
        out = await ia.fetch_post_images("https://instagram.com/reel/AAA/")
    assert out == []


@pytest.mark.asyncio
async def test_empty_response_returns_empty():
    with _patch_post(_resp(200, json_body=[])):
        out = await ia.fetch_post_images("https://instagram.com/p/AAA/")
    assert out == []


@pytest.mark.asyncio
async def test_sidecar_without_usable_displayurls_returns_empty():
    body = [
        {
            "type": "Sidecar",
            "shortCode": "AAA",
            "childPosts": [{"displayUrl": ""}, {"displayUrl": None}],
        }
    ]
    with _patch_post(_resp(200, json_body=body)):
        out = await ia.fetch_post_images("https://instagram.com/p/AAA/")
    assert out == []


# ─── failure modes ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403, 408, 500, 502])
@pytest.mark.asyncio
async def test_error_statuses_return_empty(status):
    with _patch_post(_resp(status, text="err")):
        out = await ia.fetch_post_images("https://instagram.com/p/AAA/")
    assert out == []


@pytest.mark.asyncio
async def test_network_error_returns_empty():
    err = httpx.ConnectError("boom")
    with _patch_post(err):
        out = await ia.fetch_post_images("https://instagram.com/p/AAA/")
    assert out == []


# ─── link_resolver wiring ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_resolver_dispatches_to_apify_for_instagram(monkeypatch):
    mock = AsyncMock(return_value=["https://scontent.cdninstagram.com/v/s1.jpg"])
    monkeypatch.setattr("app.channels.instagram_apify.fetch_post_images", mock)
    out = await lr.resolve("https://www.instagram.com/p/AAA/")
    mock.assert_awaited_once_with("https://www.instagram.com/p/AAA/")
    assert out == ["https://scontent.cdninstagram.com/v/s1.jpg"]


@pytest.mark.asyncio
async def test_link_resolver_caches_instagram_result(monkeypatch):
    mock = AsyncMock(return_value=["https://scontent.cdninstagram.com/v/s1.jpg"])
    monkeypatch.setattr("app.channels.instagram_apify.fetch_post_images", mock)
    await lr.resolve("https://www.instagram.com/p/AAA/")
    await lr.resolve("https://www.instagram.com/p/AAA/")
    # Second call must hit the cache, not Apify.
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_link_resolver_empty_when_apify_returns_nothing(monkeypatch):
    monkeypatch.setattr(
        "app.channels.instagram_apify.fetch_post_images",
        AsyncMock(return_value=[]),
    )
    out = await lr.resolve("https://www.instagram.com/p/AAA/")
    assert out == []
