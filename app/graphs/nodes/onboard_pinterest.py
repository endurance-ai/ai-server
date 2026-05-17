"""Onboarding Stage 4 — Pinterest URL ingest (onboarding-path).

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-001/002/004/005/006 +
REQ-ONBOARD-COMPLETION-001.

Two sub-stages keyed off `sess.onboard_stage`:

  (a) Entry — first turn after `onboard_fit done`. Card has not been shown yet
      and the user's previous callback was `fit:done`. We render the prompt
      card here and await text/callback.

  (b) Subsequent turn — user has already seen the card:
      - Callback `onboard:pinterest:skip` → complete onboarding card-only.
      - Callback `onboard:pinterest:url_mode` → ack and stay (await text).
      - Text → classify; on NONE track strikes (auto-skip after 3rd); on
        BOARD/PROFILE/PINS run shared ingest helper with
        `continuous_origin=False`, stash, then complete onboarding.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import logging
from typing import Any

from app.channels.lang import session_lang
from app.channels.onboarding_cards import (
    build_pinterest_card,
    parse_onboard_callback,
)
from app.channels.pinterest_url import (
    PinInputBoard,
    PinInputNone,
    PinInputProfile,
    classify_pinterest_input,
)
from app.core.config import settings
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.nodes._onboard_helpers import complete_onboarding
from app.graphs.nodes._onboard_stage import _emit_onboard_select
from app.graphs.nodes._pinterest_helpers import ingest_pinterest_pins
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import get_taste_store, user_key_for
from app.observability.langfuse import observe
from app.observability.langfuse import update_current_span as update_current_observation
from app.providers.apify import run_pinterest_scrape

logger = logging.getLogger(__name__)


_STRIKE_KEY = "pinterest_strikes"
_MAX_STRIKES = 3


_INVALID_URL_KO = "Pinterest URL이 안 보여요. 다시 보내주세요."
_INVALID_URL_EN = "I can't find a Pinterest URL there. Try again."
_DEGRADED_KO = "Pinterest 인증 정보가 없어요. 핀 URL을 직접 보내주시면 분석할 수 있어요."
_DEGRADED_EN = "Pinterest credentials unavailable. Send pin URLs directly and I can still analyze."


def _read_strikes(sess: Any) -> int:
    raw = getattr(sess, "onboard_selections", None) or {}
    if not isinstance(raw, dict):
        return 0
    val = raw.get(_STRIKE_KEY, 0)
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _bump_strikes(sess: Any) -> int:
    raw = getattr(sess, "onboard_selections", None)
    if not isinstance(raw, dict):
        raw = {"mood": [], "color": [], "fit": []}
    n = _read_strikes(sess) + 1
    raw[_STRIKE_KEY] = n
    sess.onboard_selections = raw
    return n


async def _link_resolver_batch_adapter(urls: list[str], *, concurrency: int) -> list[str]:
    """Thin wrapper around `link_resolver.resolve_batch` for the ingest helper."""
    from app.channels.link_resolver import resolve_batch as _rb

    return await _rb(list(urls), concurrency=concurrency)


async def _apify_adapter(url: str, *, mode: str, max_items: int) -> list[dict]:
    """Thin wrapper around `app.providers.apify.run_pinterest_scrape`."""
    if mode not in ("board", "profile"):
        return []
    return await run_pinterest_scrape(url, mode, max_items=max_items)  # type: ignore[arg-type]


async def _finalize_onboarding(state: WorkingState) -> str:
    """Run `complete_onboarding` and return the dispatch text."""
    sess = get_store().get_or_create(state.chat_id)
    user_key = user_key_for(state.from_user_id, state.chat_id)
    taste_store = get_taste_store()
    try:
        text, _ = await complete_onboarding(
            state,
            sess=sess,
            user_key=user_key,
            session_store=get_store(),
            taste_store=taste_store,
        )
        return text
    except Exception:  # noqa: BLE001
        logger.exception("🐱 [ONBOARD] complete_onboarding failed at pinterest finalize")
        return ""


async def _show_card(state: WorkingState, sess: Any, adapter: Any, lang: str) -> int | None:
    text, kb = build_pinterest_card(lang)
    try:
        msg_id = await adapter.send_text_with_keyboard(state.chat_id, text, kb)
        if msg_id:
            sess.onboard_card_message_id = msg_id
            get_store().update(sess)
        return msg_id
    except Exception:  # noqa: BLE001
        logger.exception("🐱 [ONBOARD] pinterest card send failed")
        return None


# @MX:ANCHOR: [AUTO] Stage 4 onboarding-path Pinterest handler.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-PINTEREST-006 — onboarding path must NOT call seed
#   directly here. Weights stash → completion helper performs the single
#   combined seed call. Pinned by test_completion_flow.py.
@observe(name="onboarding.stage.pinterest", as_type="span")
async def onboard_pinterest(state: WorkingState) -> dict:
    """Stage 4 Pinterest URL ingest entry / handler.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    adapter = get_adapter()
    msg = state.message
    cb_raw = msg.callback_data or ""
    text_in = (msg.text or "").strip()

    try:
        update_current_observation(metadata={"lang": lang, "stage": "pinterest"})
    except Exception:  # noqa: BLE001
        pass

    # ── Entry (no card shown yet) ────────────────────────────────────────────
    # Heuristic: when the previous stage was fit and the user just advanced via
    # done/skip, we receive a callback `onboard:fit:*`. The first onboard_pinterest
    # invocation in this turn means we should render the card.
    if not cb_raw.startswith("onboard:pinterest:") and not text_in:
        await _show_card(state, sess, adapter, lang)
        sess.onboard_stage = "pinterest"
        get_store().update(sess)
        return {
            "onboard_stage": "pinterest",
            "log_events": ["onboard_pinterest: card_sent"],
        }

    # ── Callback path ────────────────────────────────────────────────────────
    parsed = parse_onboard_callback(cb_raw) if cb_raw else None
    if parsed is not None and parsed.stage == "pinterest":
        # Ack the callback regardless of branch.
        try:
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:pinterest] cb ack best-effort", exc_info=True)

        if parsed.action == "skip":
            _emit_onboard_select(state, stage="pinterest", axis="pinterest_url_received", selected_values=["skipped"])
            completion_text = await _finalize_onboarding(state)
            if completion_text:
                try:
                    await adapter.send_text(state.chat_id, completion_text)
                except Exception:  # noqa: BLE001
                    logger.exception("🐱 [ONBOARD] completion text send failed")
            return {
                "onboard_stage": "done",
                "onboard_pin_weights": None,
                "log_events": ["onboard_pinterest: skipped"],
            }

        if parsed.action == "url_mode":
            # User indicated they'll send a URL — just stay on stage and await text.
            sess.onboard_stage = "pinterest"
            get_store().update(sess)
            return {
                "onboard_stage": "pinterest",
                "log_events": ["onboard_pinterest: url_mode_armed"],
            }

    # ── Text path (URL classification) ───────────────────────────────────────
    if not text_in:
        # No text, no recognized callback — re-render card.
        await _show_card(state, sess, adapter, lang)
        return {
            "onboard_stage": "pinterest",
            "log_events": ["onboard_pinterest: card_resent_on_blank"],
        }

    max_pins = int(getattr(settings, "PINTEREST_MAX_PINS_PER_TURN", 20))
    classified = classify_pinterest_input(text_in, max_pins=max_pins)

    if isinstance(classified, PinInputNone):
        strikes = _bump_strikes(sess)
        get_store().update(sess)
        if strikes >= _MAX_STRIKES:
            # Auto-skip with degraded message + complete card-only.
            try:
                await adapter.send_text(state.chat_id, _DEGRADED_KO if lang == "ko" else _DEGRADED_EN)
            except Exception:  # noqa: BLE001
                logger.debug("[onboard:pinterest] degraded text best-effort", exc_info=True)
            _emit_onboard_select(
                state,
                stage="pinterest",
                axis="pinterest_url_received",
                selected_values=["auto_skipped"],
            )
            completion_text = await _finalize_onboarding(state)
            if completion_text:
                try:
                    await adapter.send_text(state.chat_id, completion_text)
                except Exception:  # noqa: BLE001
                    pass
            return {
                "onboard_stage": "done",
                "onboard_pin_weights": None,
                "log_events": [f"onboard_pinterest: auto_skip strikes={strikes}"],
            }
        try:
            await adapter.send_text(state.chat_id, _INVALID_URL_KO if lang == "ko" else _INVALID_URL_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:pinterest] invalid url toast best-effort", exc_info=True)
        return {
            "onboard_stage": "pinterest",
            "log_events": [f"onboard_pinterest: invalid_url strikes={strikes}"],
        }

    # APIFY_TOKEN missing + BOARD/PROFILE mode → degraded; suggest PIN URLs and stay.
    apify_token = (getattr(settings, "APIFY_TOKEN", "") or "").strip()
    if not apify_token and isinstance(classified, (PinInputBoard, PinInputProfile)):
        try:
            await adapter.send_text(state.chat_id, _DEGRADED_KO if lang == "ko" else _DEGRADED_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[onboard:pinterest] no-apify-token text best-effort", exc_info=True)
        return {
            "onboard_stage": "pinterest",
            "log_events": ["onboard_pinterest: no_apify_token mode=board/profile"],
        }

    # ── Ingest pipeline ──────────────────────────────────────────────────────
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
        continuous_origin=False,
        pin_weight=pin_weight,
        apify_max_items=max_items,
        apify_concurrency=concurrency,
        cache_ttl_s=cache_ttl,
    )

    mode_label = outcome.mode
    selected = [mode_label] if mode_label in ("board", "profile", "pins") else ["degraded"]
    _emit_onboard_select(state, stage="pinterest", axis="pinterest_url_received", selected_values=selected)

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

    # Always finalize after ingest attempt — even on degraded, complete with card-only.
    completion_text = await _finalize_onboarding(state)
    if completion_text:
        try:
            await adapter.send_text(state.chat_id, completion_text)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [ONBOARD] completion text send failed")

    return {
        "onboard_stage": "done",
        "onboard_pin_weights": None,
        "log_events": [
            f"onboard_pinterest: ingest mode={outcome.mode} pins={outcome.successfully_analyzed}"
            f" cache_hit={outcome.cache_hit}"
        ],
    }


__all__ = ["onboard_pinterest"]
