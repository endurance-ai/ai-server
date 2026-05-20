"""Apify Instagram post → image URL resolver.

Python port of the kikoai/app `src/domains/instagram/apify-client.ts` +
`parse-apify-response.ts` pair, narrowed to the single signal we actually
need on the ai side: a list of CDN image URLs for one Instagram post URL.

External contract (Apify):
- Actor: `apify/instagram-post-scraper` (configurable via
  settings.APIFY_INSTAGRAM_ACTOR — actor id uses `~` separator per Apify API).
- Endpoint: POST
  https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?timeout={s}
- Auth: `Authorization: Bearer $APIFY_TOKEN` header (NOT query string —
  matches kikoai/app practice to avoid access-log token leakage).
- Body: `{"username": [postUrl], "addParentData": false}`
- Returns: array of `ApifyPostItem`. For a single post input the array has
  length 0 (deleted / private / region-locked) or 1.

Behavior:
- No token configured → returns [] immediately (preserves the P2-skip behavior
  callers see when Apify is unavailable). Caller logs the reason.
- Reel (`productType == "clips"`) → returns []. Reels are explicitly out of
  scope for the photo-style recommender pipeline.
- Sidecar carousel → returns every slide's `displayUrl` in order.
- 402 (free credit exhausted) → opens an in-process circuit breaker for
  settings.APIFY_402_COOLOFF_S seconds; subsequent calls short-circuit to []
  without hitting Apify, then auto-recover after the cool-off.
- 401/403/408/network failures → return []. Never raises to the caller.

Cost model: each successful call consumes Apify credit (free $5 grants
≈10,000 single-post runs at observed pricing). The 1-hour cache in
`link_resolver._CACHE` already prevents repeated charges for the same URL.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


_SHORTCODE_RE = re.compile(r"/p/([A-Za-z0-9_-]+)")

# Module-level circuit-breaker state. Set when Apify returns 402; gates all
# subsequent calls until `time.monotonic() >= _CIRCUIT_OPEN_UNTIL`.
_CIRCUIT_OPEN_UNTIL: float = 0.0


def _extract_shortcode(url: str) -> str | None:
    m = _SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def _slide_urls_from_item(item: dict[str, Any]) -> list[str]:
    """Return ordered list of slide displayUrls. Sidecar → every child slide;
    single Image/Video post → the top-level displayUrl. Empty when the item
    has nothing usable (deleted / no media)."""
    kind = item.get("type")
    if kind == "Sidecar":
        children = item.get("childPosts") or []
        urls: list[str] = []
        for ch in children:
            if not isinstance(ch, dict):
                continue
            u = ch.get("displayUrl")
            if isinstance(u, str) and u:
                urls.append(u)
        return urls
    # Single Image or Video post → take displayUrl when present. Video posts
    # still expose a poster frame as `displayUrl`, which is what we want for
    # the vision pipeline (the embedder works on images, not video).
    u = item.get("displayUrl")
    return [u] if isinstance(u, str) and u else []


async def fetch_post_images(post_url: str) -> list[str]:
    """Resolve an Instagram post URL to a list of image (CDN) URLs.

    Returns [] on any failure (missing token, Reel, 402-circuit-open, network
    error, empty response). NEVER raises — the caller treats Instagram as a
    best-effort branch, identical to the pre-Apify P2-skip semantics.
    """
    tag = "[apify-ig]"
    token = (settings.APIFY_TOKEN or "").strip()
    if not token:
        logger.info("🔗 %s ⏭️  APIFY_TOKEN 미설정 → []", tag)
        return []

    global _CIRCUIT_OPEN_UNTIL
    now = time.monotonic()
    if now < _CIRCUIT_OPEN_UNTIL:
        remaining = int(_CIRCUIT_OPEN_UNTIL - now)
        logger.warning("🔗 %s ⏸️  circuit open (402) — %ds 후 재시도 → []", tag, remaining)
        return []

    actor = settings.APIFY_INSTAGRAM_ACTOR or "apify~instagram-post-scraper"
    url = (
        f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
        f"?timeout={int(settings.APIFY_SYNC_TIMEOUT_S)}"
    )
    body = {"username": [post_url], "addParentData": False}

    shortcode = _extract_shortcode(post_url) or "(unknown)"
    logger.info("🔗 %s 호출 시작 actor=%s shortcode=%s", tag, actor, shortcode)
    t0 = time.monotonic()

    try:
        async with httpx.AsyncClient(timeout=float(settings.APIFY_FETCH_TIMEOUT_S)) as client:
            resp = await client.post(
                url,
                json=body,
                headers={
                    "authorization": f"Bearer {token}",
                    "content-type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.warning("🔗 %s ❌ 네트워크 오류 elapsed=%dms err=%r", tag, elapsed_ms, exc)
        return []

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info("🔗 %s 응답 elapsed=%dms status=%d", tag, elapsed_ms, resp.status_code)

    if resp.status_code == 402:
        cooloff = max(60, int(settings.APIFY_402_COOLOFF_S))
        _CIRCUIT_OPEN_UNTIL = time.monotonic() + cooloff
        logger.error(
            "🔗 %s ❌ 402 payment required — free credit 소진 의심. %ds 동안 IG 호출 차단",
            tag,
            cooloff,
        )
        return []
    if resp.status_code == 408:
        logger.warning("🔗 %s ❌ 408 sync timeout (%ds 초과)", tag, settings.APIFY_SYNC_TIMEOUT_S)
        return []
    if resp.status_code in (401, 403):
        logger.error("🔗 %s ❌ %d auth failed — APIFY_TOKEN 확인", tag, resp.status_code)
        return []
    if resp.status_code >= 400:
        logger.warning("🔗 %s ❌ HTTP %d body=%s", tag, resp.status_code, resp.text[:200])
        return []

    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("🔗 %s ❌ JSON 파싱 실패 err=%r", tag, exc)
        return []

    if not isinstance(data, list):
        logger.warning("🔗 %s ❌ 응답이 배열 아님 type=%s", tag, type(data).__name__)
        return []
    if not data:
        logger.warning("🔗 %s ⚠️  빈 배열 — post 삭제/비공개/지역제한 의심 shortcode=%s", tag, shortcode)
        return []

    item = data[0]
    if not isinstance(item, dict):
        logger.warning("🔗 %s ❌ item이 dict 아님 type=%s", tag, type(item).__name__)
        return []

    # Reel guard — kikoai/app rejects productType=clips. Match that boundary.
    if (item.get("productType") or "").lower() == "clips":
        logger.info("🔗 %s ⏭️  Reel(productType=clips) → []", tag)
        return []

    urls = _slide_urls_from_item(item)
    if not urls:
        logger.warning("🔗 %s ⚠️  사용 가능한 슬라이드 없음 shortcode=%s", tag, shortcode)
        return []

    logger.info(
        "🔗 %s ✅ 완료 shortcode=%s type=%s slides=%d owner=@%s",
        tag,
        shortcode,
        item.get("type"),
        len(urls),
        item.get("ownerUsername"),
    )
    return urls


def _reset_circuit_for_tests() -> None:
    """Test seam — drop the 402 cool-off."""
    global _CIRCUIT_OPEN_UNTIL
    _CIRCUIT_OPEN_UNTIL = 0.0
