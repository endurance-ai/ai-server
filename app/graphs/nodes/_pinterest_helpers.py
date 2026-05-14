"""Shared Pinterest ingest pipeline for onboard_pinterest + pinterest_ingest.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-003, 005, 006, 007.

Public surface:
    - `_AggregatedWeights` TypedDict
    - `_IngestOutcome` dataclass
    - `_normalize_pinterest_url(url) -> str`
    - `_check_pinterest_cache(sess, normalized_url, ttl_s) -> _AggregatedWeights | None`
    - `aggregate_pin_weights(image_urls, *, pin_weight, concurrency=5) -> _AggregatedWeights`
    - `ingest_pinterest_pins(state, classifier_result, *, ...) -> _IngestOutcome`

Single-seed-call discipline (REQ-ONBOARD-PINTEREST-006 + REQ-ONBOARD-COMPLETION-001):
    - `continuous_origin=True`  → call `taste_store.seed_from_onboarding` DIRECTLY
      and DO NOT stash weights into `state.onboard_pin_weights`.
    - `continuous_origin=False` → ONLY stash weights into `state.onboard_pin_weights`.
      The completion helper (`_onboard_helpers.complete_onboarding`) makes the single
      combined seed call merging card-derived + pin-derived weights.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from app.channels.pinterest_url import (
    PinInput,
    PinInputBoard,
    PinInputNone,
    PinInputPins,
    PinInputProfile,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Output containers
# ────────────────────────────────────────────────────────────────────────────
class _AggregatedWeights(TypedDict):
    """Aggregated keyword + brand weights across N Vision-analyzed pins.

    `successfully_analyzed` lets the caller surface degraded-path UX
    (e.g. "9/10 핀 분석 완료").
    """

    keyword_weights: dict[str, float]
    brand_weights: dict[str, float]
    successfully_analyzed: int


_EMPTY_WEIGHTS: _AggregatedWeights = {
    "keyword_weights": {},
    "brand_weights": {},
    "successfully_analyzed": 0,
}


@dataclass(frozen=True)
class _IngestOutcome:
    """Per-call telemetry for the `pinterest_ingest` event payload.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """

    mode: Literal["board", "profile", "pins", "none", "degraded"]
    pin_count: int = 0
    successfully_analyzed: int = 0
    cache_hit: bool = False
    seed_called: bool = False
    aggregated_weights: _AggregatedWeights | None = None
    error: str | None = None  # populated on degraded path for log surfacing
    image_urls: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# URL normalization + cache check
# ────────────────────────────────────────────────────────────────────────────
def _normalize_pinterest_url(url: str) -> str:
    """Canonicalize a Pinterest URL for use as a cache key.

    Steps (REQ-ONBOARD-PINTEREST-007):
      - lowercase host
      - strip trailing `/`
      - drop query string + fragment

    Returns the original input on `urlsplit` failure (caller still gets a
    deterministic string — cache miss is the safe failure mode).

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or "").lower()
    if not host:
        return url.strip().lower()
    path = (parts.path or "").rstrip("/")
    return f"https://{host}{path}"


def _check_pinterest_cache(
    sess: Any,
    normalized_url: str,
    ttl_s: int,
) -> _AggregatedWeights | None:
    """24-hour cache lookup for Apify-driven (board/profile) ingest.

    Returns cached `_AggregatedWeights` iff:
      - `sess.last_pinterest_scrape_url == normalized_url`
      - `now() - sess.last_pinterest_scrape_at < ttl_s`
      - `sess.last_pinterest_pins` carries an `aggregated_weights` payload.

    Else returns None — caller proceeds to Apify+Vision.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    if not normalized_url:
        return None
    cached_url = getattr(sess, "last_pinterest_scrape_url", None)
    if cached_url != normalized_url:
        return None
    cached_at = getattr(sess, "last_pinterest_scrape_at", None)
    if cached_at is None:
        return None
    # Ensure tz-awareness — naive datetimes treated as UTC for safety.
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - cached_at).total_seconds()
    if age < 0 or age > float(ttl_s):
        return None
    payload = getattr(sess, "last_pinterest_pins", None)
    if not isinstance(payload, dict):
        return None
    raw = payload.get("aggregated_weights")
    if not isinstance(raw, dict):
        return None
    kw = raw.get("keyword_weights") or {}
    br = raw.get("brand_weights") or {}
    n = raw.get("successfully_analyzed") or 0
    if not isinstance(kw, dict) or not isinstance(br, dict):
        return None
    return {
        "keyword_weights": {str(k): float(v) for k, v in kw.items() if v},
        "brand_weights": {str(k): float(v) for k, v in br.items() if v},
        "successfully_analyzed": int(n),
    }


# ────────────────────────────────────────────────────────────────────────────
# Vision batch aggregation
# ────────────────────────────────────────────────────────────────────────────
def _extract_keywords_from_vision(vr: Any) -> tuple[set[str], set[str]]:
    """Pull (keywords, brands) from a `VisionResult` for one pin.

    Dedup at the set level so same brand across multiple items in the same
    pin contributes once (caller weights once per pin).
    """
    keywords: set[str] = set()
    brands: set[str] = set()
    items = getattr(vr, "items", None) or []
    for item in items:
        for attr in ("subcategory", "colorFamily", "fit", "category"):
            v = getattr(item, attr, "") or ""
            v = v.strip().lower()
            if v:
                keywords.add(v)
        # Brand attribution — VisionItem in v2 schema does not currently expose
        # `brand_lower`, but read defensively for forward-compat.
        brand = (getattr(item, "brand_lower", "") or getattr(item, "brand", "") or "").strip().lower()
        if brand:
            brands.add(brand)
    mood = getattr(vr, "mood", None)
    if mood is not None:
        for tag in getattr(mood, "tags", []) or []:
            label = (getattr(tag, "label", "") or "").strip().lower()
            if label:
                keywords.add(label)
    return keywords, brands


async def aggregate_pin_weights(
    image_urls: list[str],
    *,
    pin_weight: float,
    concurrency: int = 5,
    vision_extract: Any = None,
) -> _AggregatedWeights:
    """Vision-analyze N pins in parallel and aggregate keyword/brand weights.

    Per-pin failures (exception OR empty `items`) are isolated — caller still
    gets weights from successful pins. Same keyword across pins ADDS (capped
    downstream by `seed_from_onboarding`).

    Args:
        image_urls: Resolved image URLs from Apify or `link_resolver.resolve_batch`.
        pin_weight: Contribution per pin (e.g. 0.05). Total contribution is
            (`successfully_analyzed` × `pin_weight`), pre-`seed_from_onboarding` cap.
        concurrency: Max concurrent Vision calls.
        vision_extract: Override for unit tests. Default lazily imports
            `app.channels.vision.extract` to avoid a hard module dependency
            during test collection.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    if not image_urls:
        return dict(_EMPTY_WEIGHTS)  # type: ignore[return-value]

    if vision_extract is None:
        from app.channels.vision import extract as _vision_extract  # local import

        vision_extract = _vision_extract

    sem = asyncio.Semaphore(max(1, min(int(concurrency), len(image_urls))))

    async def _analyze_one(url: str) -> tuple[set[str], set[str]] | None:
        async with sem:
            try:
                vr = await vision_extract(url)
            except Exception as exc:  # noqa: BLE001 — per-pin isolation
                logger.warning("🎨 [PINTEREST] vision failed url=%s err=%s", url[:60], type(exc).__name__)
                return None
            if vr is None:
                return None
            kws, brs = _extract_keywords_from_vision(vr)
            if not kws and not brs:
                return None
            return kws, brs

    results = await asyncio.gather(
        *(_analyze_one(u) for u in image_urls),
        return_exceptions=False,
    )

    keyword_weights: dict[str, float] = {}
    brand_weights: dict[str, float] = {}
    success = 0
    for r in results:
        if r is None:
            continue
        success += 1
        kws, brs = r
        for k in kws:
            keyword_weights[k] = keyword_weights.get(k, 0.0) + float(pin_weight)
        for b in brs:
            brand_weights[b] = brand_weights.get(b, 0.0) + float(pin_weight)

    return {
        "keyword_weights": keyword_weights,
        "brand_weights": brand_weights,
        "successfully_analyzed": success,
    }


# ────────────────────────────────────────────────────────────────────────────
# Ingest pipeline — single discipline for both onboarding + continuous paths
# ────────────────────────────────────────────────────────────────────────────
async def _fetch_image_urls(
    classifier_result: PinInput,
    *,
    apify_provider: Any,
    link_resolver_batch: Any,
    apify_max_items: int,
    apify_concurrency: int,
) -> tuple[Literal["board", "profile", "pins", "none", "degraded"], list[str], str | None]:
    """Dispatch by Pinterest classification mode.

    Returns `(mode, image_urls, error_reason)`. `mode='degraded'` covers the
    Apify-failure path where Apify was tried but returned `[]`.
    """
    if isinstance(classifier_result, PinInputNone):
        return "none", [], "classifier_none"

    if isinstance(classifier_result, PinInputBoard):
        try:
            items = await apify_provider(
                classifier_result.url,
                mode="board",
                max_items=apify_max_items,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("🎨 [PINTEREST] apify board failed: %s", type(exc).__name__)
            return "degraded", [], f"apify_{type(exc).__name__}"
        urls = [it.get("image_url") for it in items if isinstance(it, dict) and it.get("image_url")]
        if not urls:
            return "degraded", [], "apify_empty_board"
        return "board", urls, None

    if isinstance(classifier_result, PinInputProfile):
        try:
            items = await apify_provider(
                classifier_result.url,
                mode="profile",
                max_items=apify_max_items,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("🎨 [PINTEREST] apify profile failed: %s", type(exc).__name__)
            return "degraded", [], f"apify_{type(exc).__name__}"
        urls = [it.get("image_url") for it in items if isinstance(it, dict) and it.get("image_url")]
        if not urls:
            return "degraded", [], "apify_empty_profile"
        return "profile", urls, None

    if isinstance(classifier_result, PinInputPins):
        try:
            urls = await link_resolver_batch(
                list(classifier_result.urls),
                concurrency=apify_concurrency,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("🎨 [PINTEREST] resolve_batch failed: %s", type(exc).__name__)
            return "degraded", [], f"resolve_{type(exc).__name__}"
        if not urls:
            return "degraded", [], "resolve_empty"
        return "pins", urls, None

    return "none", [], "unknown_classifier"


def _classifier_normalize_target(classifier_result: PinInput) -> str | None:
    """Cache-key target URL — only board / profile are cacheable.

    PINS mode is exempt per SPEC L516: link_resolver already caches per-URL
    independently and Apify is not invoked.
    """
    if isinstance(classifier_result, PinInputBoard):
        return _normalize_pinterest_url(classifier_result.url)
    if isinstance(classifier_result, PinInputProfile):
        return _normalize_pinterest_url(classifier_result.url)
    return None


def _persist_cache(
    sess: Any,
    *,
    normalized_url: str,
    image_urls: list[str],
    aggregated: _AggregatedWeights,
    session_store: Any,
) -> None:
    """Write the 24h cache columns. Best-effort; cache miss is the safe fallback."""
    try:
        sess.last_pinterest_scrape_url = normalized_url
        sess.last_pinterest_scrape_at = datetime.now(UTC)
        sess.last_pinterest_pins = {
            "image_urls": list(image_urls)[:80],
            "aggregated_weights": {
                "keyword_weights": dict(aggregated["keyword_weights"]),
                "brand_weights": dict(aggregated["brand_weights"]),
                "successfully_analyzed": int(aggregated["successfully_analyzed"]),
            },
        }
        session_store.update(sess)
    except Exception:  # noqa: BLE001 — non-fatal: cache miss is acceptable
        logger.debug("🎨 [PINTEREST] cache persist best-effort skipped", exc_info=True)


def _seed_keyword_brand_dict(aggregated: _AggregatedWeights) -> dict[str, float]:
    """Flatten into the single dict shape expected by `seed_from_onboarding`.

    Brand weights are inlined under the same dict with a `brand:` prefix so the
    seed call receives one merged structure. Downstream code does NOT need to
    know the distinction — it's all additive into liked_keywords.
    """
    merged: dict[str, float] = {}
    for k, v in aggregated["keyword_weights"].items():
        if v <= 0:
            continue
        merged[k] = merged.get(k, 0.0) + float(v)
    for b, v in aggregated["brand_weights"].items():
        if v <= 0:
            continue
        key = f"brand:{b}"
        merged[key] = merged.get(key, 0.0) + float(v)
    return merged


# @MX:ANCHOR: [AUTO] Single-seed-call discipline boundary. The `continuous_origin`
#   branch is the difference between "direct seed" (continuous Pinterest) and
#   "stash for completion merge" (onboarding flow).
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-PINTEREST-006 + REQ-ONBOARD-COMPLETION-001 — exactly-one
#   seed call invariant is enforced by this branch. Tests in
#   test_pinterest_ingest.py and test_completion_flow.py both pin this contract.
async def ingest_pinterest_pins(
    state: Any,
    classifier_result: PinInput,
    *,
    apify_provider: Any,
    link_resolver_batch: Any,
    session_store: Any,
    taste_store: Any,
    sess: Any,
    user_key: str,
    continuous_origin: bool,
    pin_weight: float,
    apify_max_items: int = 80,
    apify_concurrency: int = 5,
    cache_ttl_s: int = 86400,
) -> _IngestOutcome:
    """Shared Pinterest ingest pipeline.

    Stages:
      1. Cache check (board/profile only).
      2. Apify scrape (board/profile) OR link_resolver_batch (pins).
      3. Vision batch aggregation.
      4. Cache persist (board/profile success).
      5. Seed branch:
           - continuous_origin=True  → call taste_store.seed_from_onboarding NOW.
           - continuous_origin=False → stash into state.onboard_pin_weights ONLY.

    Args:
        state: WorkingState-like object with attribute setter for
            `onboard_pin_weights`. `setattr` is used (the helper is state-shape
            agnostic for testability).
        classifier_result: Output of `classify_pinterest_input(text)`.
        apify_provider: Callable matching
            `async (url, *, mode, max_items) -> list[dict]`.
        link_resolver_batch: Callable matching
            `async (urls: list[str], *, concurrency: int) -> list[str]`.
        session_store: SessionStore — used for cache persist.
        taste_store: TasteProfileStore — used iff continuous_origin=True.
        sess: Session row for the user — used for cache check + persist.
        user_key: Output of `user_key_for(from_user_id, chat_id)`.
        continuous_origin: True for `pinterest_ingest` node; False for
            `onboard_pinterest` node.
        pin_weight: Per-pin weight contribution (e.g. 0.05).
        apify_max_items: Cap on Apify dataset size.
        apify_concurrency: Vision concurrency cap.
        cache_ttl_s: 24h default.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    # ── Stage 1: cache check (board/profile only) ─────────────────────────
    cache_target = _classifier_normalize_target(classifier_result)
    if cache_target:
        cached = _check_pinterest_cache(sess, cache_target, cache_ttl_s)
        if cached is not None:
            logger.info(
                "🎨 [PINTEREST] cache hit url=%s pins=%d",
                cache_target[:40],
                cached["successfully_analyzed"],
            )
            outcome_mode: Literal["board", "profile"] = (
                "board" if isinstance(classifier_result, PinInputBoard) else "profile"
            )
            seed_called = False
            if continuous_origin:
                try:
                    taste_store.seed_from_onboarding(user_key, _seed_keyword_brand_dict(cached))
                    seed_called = True
                except Exception:  # noqa: BLE001 — never propagate into graph
                    logger.exception("🎨 [PINTEREST] continuous seed failed (cache hit path)")
            else:
                _stash_weights_on_state(state, cached)
            return _IngestOutcome(
                mode=outcome_mode,
                pin_count=cached["successfully_analyzed"],
                successfully_analyzed=cached["successfully_analyzed"],
                cache_hit=True,
                seed_called=seed_called,
                aggregated_weights=cached,
            )

    # ── Stage 2: fetch image URLs ─────────────────────────────────────────
    mode, image_urls, error = await _fetch_image_urls(
        classifier_result,
        apify_provider=apify_provider,
        link_resolver_batch=link_resolver_batch,
        apify_max_items=apify_max_items,
        apify_concurrency=apify_concurrency,
    )
    if mode in ("none", "degraded") or not image_urls:
        return _IngestOutcome(
            mode=mode,
            pin_count=0,
            successfully_analyzed=0,
            cache_hit=False,
            seed_called=False,
            aggregated_weights=None,
            error=error,
        )

    # ── Stage 3: Vision aggregation ───────────────────────────────────────
    aggregated = await aggregate_pin_weights(
        image_urls,
        pin_weight=pin_weight,
        concurrency=apify_concurrency,
    )
    if aggregated["successfully_analyzed"] == 0:
        return _IngestOutcome(
            mode="degraded",
            pin_count=len(image_urls),
            successfully_analyzed=0,
            cache_hit=False,
            seed_called=False,
            aggregated_weights=None,
            error="vision_all_failed",
            image_urls=list(image_urls),
        )

    # ── Stage 4: cache persist (board/profile success only) ───────────────
    if cache_target:
        _persist_cache(
            sess,
            normalized_url=cache_target,
            image_urls=image_urls,
            aggregated=aggregated,
            session_store=session_store,
        )

    # ── Stage 5: seed branch ──────────────────────────────────────────────
    seed_called = False
    if continuous_origin:
        try:
            taste_store.seed_from_onboarding(user_key, _seed_keyword_brand_dict(aggregated))
            seed_called = True
        except Exception:  # noqa: BLE001
            logger.exception("🎨 [PINTEREST] continuous seed failed")
    else:
        _stash_weights_on_state(state, aggregated)

    return _IngestOutcome(
        mode=mode,
        pin_count=len(image_urls),
        successfully_analyzed=aggregated["successfully_analyzed"],
        cache_hit=False,
        seed_called=seed_called,
        aggregated_weights=aggregated,
        image_urls=list(image_urls),
    )


def _stash_weights_on_state(state: Any, aggregated: _AggregatedWeights) -> None:
    """Onboarding path — stash for the completion-node single seed call.

    Stored as a plain dict so Pydantic/dataclass `state.onboard_pin_weights`
    survives whichever container the graph uses.
    """
    try:
        state.onboard_pin_weights = {
            "keyword_weights": dict(aggregated["keyword_weights"]),
            "brand_weights": dict(aggregated["brand_weights"]),
            "successfully_analyzed": int(aggregated["successfully_analyzed"]),
        }
    except Exception:  # noqa: BLE001 — defensive: state shape may differ in tests
        logger.debug("🎨 [PINTEREST] stash weights best-effort skipped", exc_info=True)


__all__ = [
    "_AggregatedWeights",
    "_IngestOutcome",
    "_check_pinterest_cache",
    "_normalize_pinterest_url",
    "aggregate_pin_weights",
    "ingest_pinterest_pins",
]
