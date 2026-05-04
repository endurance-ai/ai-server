"""Link resolver — Pinterest (P0) + generic og:image (P0); Instagram returns [] (P2).

Maintains a 1-hour in-memory cache keyed by the original URL string.
"""

import logging
import re
import time
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[list[str], float]] = {}
_CACHE_TTL_SECONDS = 3600.0
_TIMEOUT = 8.0
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_client: httpx.AsyncClient | None = None

_OG_RE_PROP_FIRST = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_RE_CONTENT_FIRST = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    re.IGNORECASE,
)
_OG_RE_NAME = re.compile(
    r'<meta[^>]+name=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _UA},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _extract_og_image(html: str) -> str | None:
    for rx in (_OG_RE_PROP_FIRST, _OG_RE_CONTENT_FIRST, _OG_RE_NAME):
        m = rx.search(html)
        if m:
            return m.group(1)
    return None


def _pinterest_originals(url: str) -> str:
    return url.replace("/736x/", "/originals/").replace("/236x/", "/originals/")


def _is_instagram(host: str) -> bool:
    return host.endswith("instagram.com") or host == "instagram.com"


def _is_pinterest(host: str) -> bool:
    return host == "pin.it" or host.endswith("pinterest.com")


def _cache_get(url: str) -> list[str] | None:
    entry = _CACHE.get(url)
    if entry is None:
        return None
    images, expires = entry
    if time.time() > expires:
        _CACHE.pop(url, None)
        return None
    return images


def _cache_put(url: str, images: list[str]) -> None:
    _CACHE[url] = (images, time.time() + _CACHE_TTL_SECONDS)


async def resolve(url: str) -> list[str]:
    """Resolve a URL to a list of image URLs. Returns [] on any failure."""
    cached = _cache_get(url)
    if cached is not None:
        return cached

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if _is_instagram(host):
        logger.info("instagram resolution deferred to P2 url=%s", url)
        _cache_put(url, [])
        return []

    client = _get_client()

    try:
        resp = await client.get(url)
    except httpx.HTTPError as e:
        logger.warning("link_resolver fetch failed url=%s err=%s", url, e)
        _cache_put(url, [])
        return []

    if resp.status_code >= 400:
        logger.warning("link_resolver bad status url=%s status=%d", url, resp.status_code)
        _cache_put(url, [])
        return []

    final_host = (resp.url.host or "").lower()
    og = _extract_og_image(resp.text)
    if not og:
        logger.warning("link_resolver no og:image url=%s final=%s", url, str(resp.url))
        _cache_put(url, [])
        return []

    if _is_pinterest(host) or final_host.endswith("pinterest.com"):
        og = _pinterest_originals(og)

    _cache_put(url, [og])
    return [og]
