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


# ── IG ?img_index=N honoring (260612 — 0-indexed) ──────────────────────────


def test_img_index_from_url_parses_n_as_zero_indexed():
    """260612 — Instagram uses 0-indexed `img_index` (verified live: sharing
    the 3rd slide of a carousel produces `?img_index=2`). The returned value
    is already a valid Python list index — no `n - 1` conversion needed.
    """
    from app.channels.link_resolver import _img_index_from_url

    assert _img_index_from_url("https://www.instagram.com/p/ABC/?img_index=2") == 2
    assert _img_index_from_url("https://www.instagram.com/p/ABC/?img_index=1") == 1
    assert _img_index_from_url("https://www.instagram.com/p/ABC/?img_index=5") == 5
    # img_index=0 is the first slide — valid; reorder helper just no-ops it.
    assert _img_index_from_url("https://www.instagram.com/p/ABC/?img_index=0") == 0


def test_img_index_from_url_returns_none_when_absent_or_invalid():
    from app.channels.link_resolver import _img_index_from_url

    assert _img_index_from_url("https://www.instagram.com/p/ABC/") is None
    assert _img_index_from_url("https://www.instagram.com/p/ABC/?other=foo") is None
    assert _img_index_from_url("https://www.instagram.com/p/ABC/?img_index=abc") is None
    assert _img_index_from_url("https://www.instagram.com/p/ABC/?img_index=-1") is None


def test_reorder_by_img_index_hoists_target_slide_to_front():
    """260612 — Instagram 0-indexed: `img_index=N` = (N+1)-th slide = images[N]."""
    from app.channels.link_resolver import _reorder_by_img_index

    images = ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
    # img_index=2 → c.jpg (3rd slide) hoisted to position 0.
    out = _reorder_by_img_index("https://www.instagram.com/p/X/?img_index=2", images)
    assert out == ["c.jpg", "a.jpg", "b.jpg", "d.jpg"]
    # img_index=1 → b.jpg (2nd slide) hoisted.
    out = _reorder_by_img_index("https://www.instagram.com/p/X/?img_index=1", images)
    assert out == ["b.jpg", "a.jpg", "c.jpg", "d.jpg"]
    # img_index=3 → d.jpg (4th slide) hoisted.
    out = _reorder_by_img_index("https://www.instagram.com/p/X/?img_index=3", images)
    assert out == ["d.jpg", "a.jpg", "b.jpg", "c.jpg"]


def test_reorder_by_img_index_passthrough_on_edge_cases():
    from app.channels.link_resolver import _reorder_by_img_index

    images = ["a.jpg", "b.jpg", "c.jpg"]
    # No img_index → unchanged.
    assert _reorder_by_img_index("https://www.instagram.com/p/X/", images) == images
    # img_index=0 (already at front) → unchanged.
    assert _reorder_by_img_index("https://www.instagram.com/p/X/?img_index=0", images) == images
    # Out-of-range index → fall back to the original order (do not crash).
    assert _reorder_by_img_index("https://www.instagram.com/p/X/?img_index=99", images) == images
    # Single image → unchanged regardless of index.
    assert _reorder_by_img_index("https://www.instagram.com/p/X/?img_index=2", ["only.jpg"]) == ["only.jpg"]
    # Empty list → empty.
    assert _reorder_by_img_index("https://www.instagram.com/p/X/?img_index=2", []) == []
