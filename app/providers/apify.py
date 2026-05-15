"""Apify Pinterest scraper — async HTTP wrapper.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-005, REQ-ONBOARD-SEC-001.

Calls the Apify `epctex/pinterest-scraper` actor via the REST sync-get-dataset-items
endpoint (https://docs.apify.com/api/v2#/reference/actors/run-actor-synchronously-and-get-dataset-items).

Design notes
------------
- Uses `httpx.AsyncClient` directly (no `apify-client` SDK dep) — keeps the
  dependency surface minimal and lets us reuse the project's existing httpx
  posture.
- Graceful degrade: missing token / network error → returns `[]`. Only the
  explicit `asyncio.TimeoutError` path raises `ApifyTimeoutError` so the
  graph layer can distinguish "user has no creds" from "actor is slow".
- Per-pin failure tolerance: individual malformed dataset items are skipped,
  not propagated as exceptions.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class ApifyTimeoutError(Exception):
    """Raised when the actor run exceeds the per-call timeout budget.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """


def _safe_log_url(url: str) -> str:
    """Redact URL for logs — host + first 8 chars of path only (REQ-ONBOARD-SEC-001).

    Avoid leaking user-identifying Pinterest profile / board slugs into log
    aggregation. Token is NEVER logged — caller is responsible for not
    interpolating headers into log strings.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return "<malformed-url>"
    host = parsed.host or "?"
    path = (parsed.path or "")[:8]
    return f"{host}{path}…"


def _normalize_item(item: dict) -> dict | None:
    """Coerce a raw Apify dataset item into our `{image_url, description, link}` shape.

    Returns `None` for items lacking an image — caller filters those out so
    one bad pin in the dataset never poisons the whole batch (REQ-ONBOARD-PINTEREST-005).
    """
    image_url = item.get("imageUrl") or item.get("image_url") or item.get("image") or item.get("media")
    if not image_url or not isinstance(image_url, str):
        return None
    description = item.get("description") or item.get("title") or ""
    link = item.get("link") or item.get("pinUrl") or item.get("url") or ""
    return {
        "image_url": image_url,
        "description": description if isinstance(description, str) else "",
        "link": link if isinstance(link, str) else "",
    }


async def run_pinterest_scrape(
    url: str,
    mode: Literal["board", "profile"],
    max_items: int = 80,
) -> list[dict]:
    """Run the Pinterest scraper actor synchronously and return the dataset items.

    Args:
        url: Canonical board or profile URL (caller is responsible for normalization
            via `app.channels.pinterest_url.classify_pinterest_input`).
        mode: `"board"` or `"profile"` — passed through to the actor input.
        max_items: Per-call cap, defaults to `APIFY_PINTEREST_MAX_ITEMS` semantics.

    Returns:
        List of `{image_url, description, link}` dicts. Empty list on any
        non-timeout failure (missing token, network error, actor failure,
        empty dataset, all items malformed).

    Raises:
        ApifyTimeoutError: When the per-call timeout (`30s`) is exceeded.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    token = (settings.APIFY_TOKEN or "").strip()
    if not token:
        logger.info("🎨 [APIFY][degraded] reason=no_token mode=%s", mode)
        return []

    actor_id = settings.APIFY_PINTEREST_ACTOR.replace("/", "~")
    # @MX:NOTE: Token MUST be passed via Authorization header, NOT URL query
    # string — Apify access logs / network proxies / httpx DEBUG dumps would
    # otherwise leak the credential (security review P0-01).
    endpoint = f"https://api.apify.com/v2/acts/{actor_id}/run-sync-get-dataset-items"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "startUrls": [{"url": url}],
        "maxItems": max(1, min(int(max_items), 100)),
        "mode": mode,
    }
    timeout = 30.0
    safe_url = _safe_log_url(url)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await asyncio.wait_for(
                client.post(endpoint, json=payload, headers=headers),
                timeout=timeout,
            )
    except TimeoutError as exc:
        logger.info("🎨 [APIFY][timeout] mode=%s url=%s", mode, safe_url)
        raise ApifyTimeoutError(f"Apify actor exceeded {timeout}s for {safe_url}") from exc
    except httpx.HTTPError as exc:
        logger.info("🎨 [APIFY][degraded] reason=%s mode=%s url=%s", type(exc).__name__, mode, safe_url)
        return []
    except Exception as exc:  # noqa: BLE001 — defensive: NEVER raise into graph
        logger.info("🎨 [APIFY][degraded] reason=%s mode=%s", type(exc).__name__, mode)
        return []

    if resp.status_code >= 400:
        logger.info("🎨 [APIFY][degraded] status=%d mode=%s url=%s", resp.status_code, mode, safe_url)
        return []

    try:
        data = resp.json()
    except (ValueError, httpx.DecodingError):
        logger.info("🎨 [APIFY][degraded] reason=bad_json mode=%s", mode)
        return []

    if not isinstance(data, list):
        return []

    # Per-pin failure tolerance — skip malformed items, keep good ones.
    normalized: list[dict] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        item = _normalize_item(raw)
        if item is not None:
            normalized.append(item)
    logger.info(
        "🎨 [APIFY] ok mode=%s url=%s pins=%d (raw=%d)",
        mode,
        safe_url,
        len(normalized),
        len(data),
    )
    return normalized
