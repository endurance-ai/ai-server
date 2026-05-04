"""Unit tests for app/channels/link_resolver."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.channels import link_resolver


@pytest.fixture(autouse=True)
def _reset_resolver_state():
    link_resolver._CACHE.clear()
    link_resolver._client = None
    yield
    link_resolver._CACHE.clear()
    link_resolver._client = None


def _make_response(text: str, final_url: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    parsed = httpx.URL(final_url)
    resp.url = parsed
    return resp


@pytest.mark.asyncio
async def test_instagram_returns_empty(monkeypatch):
    images = await link_resolver.resolve("https://www.instagram.com/p/ABC123/")
    assert images == []


@pytest.mark.asyncio
async def test_pin_it_short_url_extracts_og_image(monkeypatch):
    html = '<html><head><meta property="og:image" content="https://i.pinimg.com/736x/aa/bb/cc/test.jpg"></head></html>'
    fake_resp = _make_response(html, "https://www.pinterest.com/pin/12345/")
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(link_resolver, "_get_client", lambda: fake_client)

    images = await link_resolver.resolve("https://pin.it/abc123")

    assert len(images) == 1
    assert "originals" in images[0]
    assert "736x" not in images[0]


@pytest.mark.asyncio
async def test_og_image_attribute_order_content_first(monkeypatch):
    html = '<meta content="https://example.com/img.jpg" property="og:image">'
    fake_resp = _make_response(html, "https://example.com/page")
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(link_resolver, "_get_client", lambda: fake_client)

    images = await link_resolver.resolve("https://example.com/page")

    assert images == ["https://example.com/img.jpg"]


@pytest.mark.asyncio
async def test_generic_no_pinterest_transform(monkeypatch):
    html = '<meta property="og:image" content="https://cdn.example.com/736x/foo.jpg">'
    fake_resp = _make_response(html, "https://example.com/page")
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(link_resolver, "_get_client", lambda: fake_client)

    images = await link_resolver.resolve("https://example.com/page")

    assert images == ["https://cdn.example.com/736x/foo.jpg"]


@pytest.mark.asyncio
async def test_no_og_image_returns_empty(monkeypatch):
    html = "<html><head><title>nothing</title></head></html>"
    fake_resp = _make_response(html, "https://example.com/page")
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(link_resolver, "_get_client", lambda: fake_client)

    images = await link_resolver.resolve("https://example.com/page")

    assert images == []


@pytest.mark.asyncio
async def test_http_error_returns_empty(monkeypatch):
    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    monkeypatch.setattr(link_resolver, "_get_client", lambda: fake_client)

    images = await link_resolver.resolve("https://example.com/page")

    assert images == []


@pytest.mark.asyncio
async def test_cache_hit_skips_http(monkeypatch):
    html = '<meta property="og:image" content="https://cdn.example.com/img.jpg">'
    fake_resp = _make_response(html, "https://example.com/page")
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    monkeypatch.setattr(link_resolver, "_get_client", lambda: fake_client)

    first = await link_resolver.resolve("https://example.com/page")
    second = await link_resolver.resolve("https://example.com/page")

    assert first == second
    assert fake_client.get.await_count == 1
