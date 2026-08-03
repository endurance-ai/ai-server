"""Unit tests for the link resolver pure helpers + no-network fastpaths.

Covers og:image extraction, Pinterest original substitution, host classifiers
(instagram / pinterest / direct-image CDN with lookalike rejection), the IG
`?img_index=N` reorder, the in-memory TTL cache, and the direct-image fastpath
in `resolve()` (which only runs the pure SSRF guard, no HTTP). Network-bound
og:image scraping is intentionally out of scope here.
"""

from __future__ import annotations

import time

import pytest

from app.channels import link_resolver as lr


@pytest.fixture(autouse=True)
def _clear_cache():
    lr._CACHE.clear()
    yield
    lr._CACHE.clear()


# ── _extract_og_image ───────────────────────────────────────────────────────


def test_extract_og_property_first():
    html = '<meta property="og:image" content="http://x/a.jpg">'
    assert lr._extract_og_image(html) == "http://x/a.jpg"


def test_extract_og_content_first():
    html = '<meta content="http://x/b.jpg" property="og:image">'
    assert lr._extract_og_image(html) == "http://x/b.jpg"


def test_extract_og_name_variant():
    html = '<meta name="og:image" content="http://x/c.jpg">'
    assert lr._extract_og_image(html) == "http://x/c.jpg"


def test_extract_og_absent_returns_none():
    assert lr._extract_og_image("<html>no meta here</html>") is None


# ── _pinterest_originals ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://i.pinimg.com/736x/a/b.jpg", "https://i.pinimg.com/originals/a/b.jpg"),
        ("https://i.pinimg.com/236x/a/b.jpg", "https://i.pinimg.com/originals/a/b.jpg"),
        ("https://i.pinimg.com/originals/a/b.jpg", "https://i.pinimg.com/originals/a/b.jpg"),
    ],
)
def test_pinterest_originals(url, expected):
    assert lr._pinterest_originals(url) == expected


# ── host classifiers ────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["instagram.com", "www.instagram.com"])
def test_is_instagram_true(host):
    assert lr._is_instagram(host) is True


def test_is_instagram_false():
    assert lr._is_instagram("example.com") is False


@pytest.mark.parametrize("host", ["pin.it", "pinterest.com", "www.pinterest.com"])
def test_is_pinterest_true(host):
    assert lr._is_pinterest(host) is True


def test_is_pinterest_false():
    assert lr._is_pinterest("example.com") is False


@pytest.mark.parametrize(
    "host",
    ["images.unsplash.com", "i.pinimg.com", "pinimg.com", "scontent.cdninstagram.com", "x.fbcdn.net"],
)
def test_is_direct_image_host_true(host):
    assert lr._is_direct_image_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["evilpinimg.com", "notcdninstagram.com", "pinimg.com.evil.com", "example.com"],
)
def test_is_direct_image_host_rejects_lookalikes(host):
    assert lr._is_direct_image_host(host) is False


# ── img_index parsing + reorder ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://instagram.com/p/abc/?img_index=2", 2),
        ("https://instagram.com/p/abc/?img_index=0", 0),
        ("https://instagram.com/p/abc/", None),
        ("https://instagram.com/p/abc/?img_index=-1", None),
        ("https://instagram.com/p/abc/?img_index=foo", None),
    ],
)
def test_img_index_from_url(url, expected):
    assert lr._img_index_from_url(url) == expected


def test_reorder_hoists_indexed_slide():
    imgs = ["a", "b", "c", "d"]
    out = lr._reorder_by_img_index("u?img_index=2", imgs)
    assert out == ["c", "a", "b", "d"]


def test_reorder_single_image_unchanged():
    assert lr._reorder_by_img_index("u?img_index=2", ["only"]) == ["only"]


def test_reorder_index_zero_unchanged():
    imgs = ["a", "b", "c"]
    assert lr._reorder_by_img_index("u?img_index=0", imgs) == imgs


def test_reorder_out_of_range_unchanged():
    imgs = ["a", "b"]
    assert lr._reorder_by_img_index("u?img_index=9", imgs) == imgs


def test_reorder_no_index_unchanged():
    imgs = ["a", "b"]
    assert lr._reorder_by_img_index("u", imgs) == imgs


# ── cache get/put + TTL ─────────────────────────────────────────────────────


def test_cache_put_then_get():
    lr._cache_put("http://x", ["img1"])
    assert lr._cache_get("http://x") == ["img1"]


def test_cache_get_miss_returns_none():
    assert lr._cache_get("http://absent") is None


def test_cache_expired_entry_evicted(monkeypatch):
    lr._cache_put("http://x", ["img1"])
    # Force the stored entry to be already expired.
    images, _ = lr._CACHE["http://x"]
    lr._CACHE["http://x"] = (images, time.time() - 1.0)
    assert lr._cache_get("http://x") is None
    assert "http://x" not in lr._CACHE


# ── resolve() direct-image fastpath + cache hit (no network) ────────────────


async def test_resolve_direct_image_fastpath_returns_url():
    url = "https://images.unsplash.com/photo-abc123"
    out = await lr.resolve(url)
    assert out == [url]
    # second call hits the cache.
    assert lr._cache_get(url) == [url]


async def test_resolve_cache_hit_short_circuits():
    lr._cache_put("https://example.com/page", ["cached-img"])
    out = await lr.resolve("https://example.com/page")
    assert out == ["cached-img"]


async def test_resolve_batch_empty_returns_empty():
    assert await lr.resolve_batch([]) == []


async def test_resolve_batch_flattens_direct_images():
    urls = [
        "https://images.unsplash.com/photo-1",
        "https://i.pinimg.com/originals/a/b.jpg",
    ]
    out = await lr.resolve_batch(urls)
    assert out == urls
