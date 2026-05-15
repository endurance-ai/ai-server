"""Continuous Pinterest bootstrap node (post-onboarding).

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-003 + REQ-ONBOARD-PINTEREST-007.

Triggered when an *already-onboarded* user sends a Pinterest URL outside the
onboarding flow. The routing predicate that gates entry is added in Phase 4
(ONB-T18); this node only encodes the per-turn semantics:

  1. Rate-limit window check: if `now() - sess.last_pinterest_scrape_at <
     PINTEREST_CONTINUOUS_RATELIMIT_S` send a polite "try later" reply and
     return (no state mutation).
  2. Classify the text via `classify_pinterest_input`.
  3. Run the shared ingest helper with `continuous_origin=True` — it makes the
     DIRECT seed call (no completion phase) and may hit cache (24h).
  4. Send a sticky-lang confirmation message per mode.
  5. Critical invariant: `sess.onboarded_at` is NEVER modified.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.channels.lang import session_lang
from app.channels.pinterest_url import (
    PinInputNone,
    classify_pinterest_input,
)
from app.channels.session import get_store
from app.channels.taste_profile import get_taste_store, user_key_for
from app.core.config import settings
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.nodes._pinterest_helpers import ingest_pinterest_pins
from app.graphs.state import WorkingState
from app.observability.conversation_log import emit
from app.observability.langfuse import observe
from app.observability.langfuse import update_current_span as update_current_observation
from app.providers.apify import run_pinterest_scrape

logger = logging.getLogger(__name__)


# ── Confirmation copy by mode + lang ─────────────────────────────────────────
_CONFIRM = {
    ("board", "ko"): "📌 보드 분석해서 취향에 더했어요.",
    ("board", "en"): "📌 Analyzed your board and added it to your taste.",
    ("profile", "ko"): "📌 프로필에서 {n}핀 분석 완료.",
    ("profile", "en"): "📌 Analyzed {n} pins from your profile.",
    ("pins", "ko"): "📌 핀 {n}개 분석 완료.",
    ("pins", "en"): "📌 Analyzed {n} pins.",
    ("degraded", "ko"): "📌 핀터레스트 분석이 잠시 막혀 있어요. 잠시 후 다시 시도해 주세요.",
    ("degraded", "en"): "📌 Pinterest analysis hit a snag. Please try again later.",
    ("none", "ko"): "📌 Pinterest URL을 찾지 못했어요.",
    ("none", "en"): "📌 No Pinterest URL detected.",
}

_RATE_LIMIT_KO = "🕐 5분 후에 다시 시도해 주세요."
_RATE_LIMIT_EN = "🕐 Please try again in 5 minutes."


def _now(*, override: datetime | None = None) -> datetime:
    return override or datetime.now(UTC)


def _within_rate_limit(sess: Any, *, window_s: int, now: datetime | None = None) -> bool:
    """True iff `sess.last_pinterest_scrape_at` is set AND younger than `window_s`."""
    last = getattr(sess, "last_pinterest_scrape_at", None)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    elapsed = (_now(override=now) - last).total_seconds()
    return 0 <= elapsed < float(window_s)


async def _link_resolver_batch_adapter(urls: list[str], *, concurrency: int) -> list[str]:
    from app.channels.link_resolver import resolve_batch as _rb

    return await _rb(list(urls), concurrency=concurrency)


async def _apify_adapter(url: str, *, mode: str, max_items: int) -> list[dict]:
    if mode not in ("board", "profile"):
        return []
    return await run_pinterest_scrape(url, mode, max_items=max_items)  # type: ignore[arg-type]


def _emit_pinterest_ingest(state: WorkingState, *, mode: str, pin_count: int, vision_count: int) -> None:
    """SPEC-CONVERSATION-LOG-001 — fire `pinterest_ingest` event."""
    try:
        emit(
            event_type="pinterest_ingest",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=1,
            payload={
                "mode": mode,
                "pin_count": int(pin_count),
                "vision_results_count": int(vision_count),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[pinterest_ingest] emit best-effort", exc_info=True)


# @MX:ANCHOR: [AUTO] Continuous Pinterest bootstrap entry point.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-PINTEREST-003 — onboarded_at invariant + direct seed
#   call discipline. test_pinterest_ingest.py pins both behaviors.
@observe(name="pinterest.continuous_ingest", as_type="span")
async def pinterest_ingest(state: WorkingState) -> dict:
    """Continuous Pinterest bootstrap handler (post-onboarding).

    Returns a state delta. `state.continuous_origin` is set True for downstream
    correlation; `sess.onboarded_at` is NEVER mutated.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    adapter = get_adapter()
    msg = state.message
    text_in = (msg.text or "").strip()

    state.continuous_origin = True

    # ── Rate-limit gate ──────────────────────────────────────────────────────
    rl_window = int(getattr(settings, "PINTEREST_CONTINUOUS_RATELIMIT_S", 300))
    if rl_window > 0 and _within_rate_limit(sess, window_s=rl_window):
        try:
            await adapter.send_text(state.chat_id, _RATE_LIMIT_KO if lang == "ko" else _RATE_LIMIT_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[pinterest_ingest] rate-limit reply best-effort", exc_info=True)
        return {
            "continuous_origin": True,
            "log_events": ["pinterest_ingest: rate_limited"],
        }

    # ── Classify ─────────────────────────────────────────────────────────────
    max_pins = int(getattr(settings, "PINTEREST_MAX_PINS_PER_TURN", 20))
    classified = classify_pinterest_input(text_in, max_pins=max_pins)
    if isinstance(classified, PinInputNone):
        # Caller-side predicate should never let this happen, but defend.
        return {
            "continuous_origin": True,
            "log_events": ["pinterest_ingest: classifier_none"],
        }

    # ── Shared ingest pipeline ───────────────────────────────────────────────
    pin_weight = float(getattr(settings, "ONBOARDING_PINTEREST_PIN_WEIGHT", 0.05))
    cache_ttl = int(getattr(settings, "PINTEREST_INGEST_CACHE_TTL_S", 86400))
    max_items = int(getattr(settings, "APIFY_PINTEREST_MAX_ITEMS", 80))
    concurrency = int(getattr(settings, "APIFY_PINTEREST_CONCURRENCY", 5))
    user_key = user_key_for(state.from_user_id, state.chat_id)
    taste_store = get_taste_store()

    outcome = await ingest_pinterest_pins(
        state,
        classified,
        apify_provider=_apify_adapter,
        link_resolver_batch=_link_resolver_batch_adapter,
        session_store=get_store(),
        taste_store=taste_store,
        sess=sess,
        user_key=user_key,
        continuous_origin=True,
        pin_weight=pin_weight,
        apify_max_items=max_items,
        apify_concurrency=concurrency,
        cache_ttl_s=cache_ttl,
    )

    # ── Confirmation message ─────────────────────────────────────────────────
    mode_key = outcome.mode if outcome.mode in ("board", "profile", "pins", "degraded") else "none"
    template = _CONFIRM.get((mode_key, lang)) or _CONFIRM.get((mode_key, "en"), "")
    if template:
        text = template.format(n=outcome.successfully_analyzed)
        try:
            await adapter.send_text(state.chat_id, text)
        except Exception:  # noqa: BLE001
            logger.debug("[pinterest_ingest] confirm send best-effort", exc_info=True)

    try:
        update_current_observation(
            metadata={
                "url_mode": outcome.mode,
                "pin_count": outcome.pin_count,
                "cache_hit": outcome.cache_hit,
                "lang": lang,
            }
        )
    except Exception:  # noqa: BLE001
        pass

    _emit_pinterest_ingest(
        state,
        mode=outcome.mode,
        pin_count=outcome.pin_count,
        vision_count=outcome.successfully_analyzed,
    )

    # NB: `sess.onboarded_at` MUST NOT be modified here (REQ-ONBOARD-PINTEREST-003).
    return {
        "continuous_origin": True,
        "log_events": [
            f"pinterest_ingest: mode={outcome.mode} pins={outcome.successfully_analyzed} cache_hit={outcome.cache_hit}"
        ],
    }


__all__ = ["pinterest_ingest"]
