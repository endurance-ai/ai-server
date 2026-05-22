"""Link resolver — Pinterest (P0) + generic og:image (P0); Instagram returns [] (P2).

Maintains a 1-hour in-memory cache keyed by the original URL string.
"""

import asyncio
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from app.channels.schemas import _ssrf_guard_url

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
        # follow_redirects=False — 수동으로 각 hop마다 SSRF 재검증한다
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": _UA},
        )
    return _client


_MAX_REDIRECTS = 5


async def _safe_get(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    """SSRF guard를 적용해 redirect 체인을 수동으로 따라간다.
    각 hop의 호스트가 내부망/링크-로컬/cloud metadata IP면 즉시 중단."""
    current = url
    for _ in range(_MAX_REDIRECTS):
        try:
            _ssrf_guard_url(current)
        except ValueError as e:
            logger.warning("🔗 [LINK] 🛡️  SSRF 차단 url=%s err=%s", current, e)
            return None
        try:
            resp = await client.get(current)
        except httpx.HTTPError as e:
            logger.warning("🔗 [LINK] ❌ 페치 실패 url=%s err=%s", current, e)
            return None
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("location")
            if not loc:
                return resp
            current = str(httpx.URL(current).join(loc))
            continue
        return resp
    logger.warning("🔗 [LINK] ⚠️  redirect 횟수 초과 (%d) url=%s", _MAX_REDIRECTS, url)
    return None


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


# P1-3 (260521 V3 eval): direct-image CDN hosts. URLs on these hosts ARE the
# image (no HTML page to scrape og:image from), so og:image fetch returns ""
# and the resolver fails. Short-circuit: accept the URL verbatim (still SSRF-
# guarded by the caller's existing scheme/IP guard). Suffix matching on the
# host lets *.cdninstagram.com / *.pinimg.com etc. all hit.
_DIRECT_IMAGE_HOSTS: tuple[str, ...] = (
    "images.unsplash.com",
    "i.pinimg.com",
    "pinimg.com",
    "cdninstagram.com",     # *.cdninstagram.com
    "fbcdn.net",            # *.fbcdn.net (Meta image CDN)
    "pbs.twimg.com",
    "media.discordapp.net",
)


def _is_direct_image_host(host: str) -> bool:
    # Exact match OR proper subdomain (dot-prefixed) only. The bare
    # `endswith(d)` form was removed (review 260522): it let lookalike hosts
    # like `evilpinimg.com` / `notcdninstagram.com` satisfy the allowlist.
    # `_ssrf_guard_url` still runs downstream, but the allowlist itself must
    # not be bypassable.
    h = host.lower()
    return any(h == d or h.endswith("." + d) for d in _DIRECT_IMAGE_HOSTS)


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
    """URL → 이미지 URL 리스트 변환. 실패 시 [] 반환 (예외 던지지 않음)."""
    cached = _cache_get(url)
    if cached is not None:
        logger.info("🔗 [LINK] 캐시 히트 url=%s images=%d", url, len(cached))
        return cached

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    logger.info("🔗 [LINK] 시작 host=%s url=%s", host, url)

    # P1-3 (260521 V3 eval): known direct-image CDN → URL IS the image, no
    # og:image scrape needed. Without this, Unsplash photo URLs and IG CDN
    # URLs both returned [] (og:image absent) → vision skipped → agent fell
    # back to text-only path that leaked the prior turn's pending_question.
    if _is_direct_image_host(host):
        try:
            _ssrf_guard_url(url)
        except ValueError as e:
            logger.warning("🔗 [LINK] 🛡️  direct-image SSRF 차단 url=%s err=%s", url, e)
            _cache_put(url, [])
            return []
        logger.info("🔗 [LINK] ✅ direct-image fastpath host=%s", host)
        _cache_put(url, [url])
        return [url]

    if _is_instagram(host):
        # Apify-backed Instagram resolver (mirrors kikoai/app's main-flow v2).
        # When APIFY_TOKEN is unset or Apify fails (402/408/network/Reel), the
        # function returns [] — identical to the previous P2-skip contract, so
        # the caller's fallback path is unchanged.
        from app.channels.instagram_apify import fetch_post_images

        images = await fetch_post_images(url)
        # SSRF-guard each returned CDN URL (Instagram CDN host is public, but
        # the guard catches malformed/scheme-less surprises). Drop offenders
        # silently — we never raise from the link resolver.
        safe: list[str] = []
        for u in images:
            try:
                _ssrf_guard_url(u)
                safe.append(u)
            except ValueError as e:
                logger.warning("🔗 [LINK] 🛡️  IG slide SSRF 차단 url=%s err=%s", u, e)
        _cache_put(url, safe)
        return safe

    client = _get_client()

    resp = await _safe_get(client, url)
    if resp is None:
        _cache_put(url, [])
        return []

    if resp.status_code >= 400:
        logger.warning("🔗 [LINK] 응답 에러 url=%s status=%d", url, resp.status_code)
        _cache_put(url, [])
        return []

    final_host = (resp.url.host or "").lower()
    if str(resp.url) != url:
        logger.info("🔗 [LINK]   ↳ 🔁 리다이렉트 → %s", str(resp.url))

    og = _extract_og_image(resp.text)
    if not og:
        logger.warning("🔗 [LINK] og:image 못 찾음 final=%s body_len=%d", str(resp.url), len(resp.text))
        _cache_put(url, [])
        return []

    if _is_pinterest(host) or final_host.endswith("pinterest.com"):
        og_orig = _pinterest_originals(og)
        if og_orig != og:
            logger.info("🔗 [LINK]   ↳ 🖼️  pinterest 풀해상도로 치환")
        og = og_orig

    # 추출된 og:image도 SSRF 체크 — Telegram이 이걸 가져갈 거니까
    try:
        _ssrf_guard_url(og)
    except ValueError as e:
        logger.warning("🔗 [LINK] 🛡️  og:image SSRF 차단 url=%s err=%s", og, e)
        _cache_put(url, [])
        return []

    logger.info("🔗 [LINK] ✅ 완료 og:image=%s", og)
    _cache_put(url, [og])
    return [og]


# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# REQ-ONBOARD-PINTEREST-005 mode C — batch resolution preserving existing
# single-URL semantics. Per-URL failures are silently omitted from the result
# (NOT raised as exceptions). All SSRF / redirect / cache / Pinterest originals
# policies are inherited from the single-URL `resolve()` since we delegate
# straight to it under an asyncio.Semaphore.
async def resolve_batch(urls: list[str], concurrency: int = 5) -> list[str]:
    """Resolve multiple URLs in parallel with a concurrency cap.

    Each URL goes through the existing `resolve()` pipeline (SSRF, redirect,
    og:image, Pinterest originals substitution, 1h cache). Per-URL failures
    are silently dropped — callers receive only successful image URLs.

    Args:
        urls: Input URLs (may be a mix of Pinterest pins, generic web pages, etc.)
        concurrency: Max concurrent `resolve()` calls. Defaults to 5.

    Returns:
        Flat list of image URLs. Order preservation is best-effort (asyncio.gather
        preserves order across the outer list, but each `resolve()` returns 0..1
        URLs so the resulting flat list mirrors successful inputs in order).
    """
    if not urls:
        return []
    capped = max(1, min(int(concurrency), len(urls)))
    sem = asyncio.Semaphore(capped)

    async def _one(u: str) -> list[str]:
        async with sem:
            try:
                return await resolve(u)
            except Exception as exc:  # noqa: BLE001 — defensive: never propagate
                logger.warning("🔗 [LINK][batch] resolve failed url=%s err=%s", u, exc)
                return []

    results = await asyncio.gather(*(_one(u) for u in urls), return_exceptions=False)
    out: list[str] = []
    for r in results:
        if r:
            out.extend(r)
    return out
