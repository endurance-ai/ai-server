"""SPEC-AGENT-001 / REQ-AGENT-004 (node 6/10) — critique_apply.

Two paths:
1. Callback `crit:*` → `critique.parse_callback(callback_data, last_results)`.
   Reinforces taste profile (more=liked / less=disliked).
2. Free text → uses `state.decision.critique_delta` (router output) OR
   constructs a free-text delta from the raw text.

Writes:
- `critique_delta` (consumed by search_node)
- `presearch_summary` (plan.md Q6 — one-line summary, used by respond)
- `messages: [SystemMessage]` breadcrumb (plan.md Q4)

Wraps: `app/channels/critique.py::parse_callback` + the existing `_summarize_delta`
logic from scenario.py (lifted to a private helper here).
"""

from __future__ import annotations

import logging

from langchain_core.messages import SystemMessage

from app.channels.critique import CritiqueDelta, parse_callback
from app.channels.router import RoutedIntent
from app.channels.session import SessionState, get_store
from app.channels.taste_profile import (
    TasteProfile,
    get_taste_store,
    user_key_for,
)
from app.core.config import settings
from app.graphs.state import WorkingState
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

_MAX_INTENT_LEN = 512


def format_delta_summary(delta: CritiqueDelta) -> str:
    """One-line natural-language summary — also reused by Langfuse trace
    metadata (REQ-OBSV-003 row 3)."""
    bits: list[str] = []
    if delta.exclude_keywords:
        bits.append("less " + ", ".join(delta.exclude_keywords[:2]))
    if delta.exclude_brands:
        bits.append("not " + ", ".join(delta.exclude_brands[:2]))
    if delta.boost_keywords:
        bits.append("more " + ", ".join(delta.boost_keywords[:2]))
    if delta.color:
        bits.append(f"in {delta.color}")
    if delta.max_price is not None:
        bits.append(f"under ₩{delta.max_price:,}")
    if delta.min_price is not None:
        bits.append(f"over ₩{delta.min_price:,}")
    if not bits and delta.extra_intent:
        bits.append(delta.extra_intent[:60])
    return ", ".join(bits) if bits else "your tweak"


def _reinforce_taste(sess, profile: TasteProfile | None, delta: CritiqueDelta) -> None:
    if profile is None or delta.anchor is None or not delta.anchor.brand:
        return
    if delta.op == "more":
        profile.reinforce_liked_brand(delta.anchor.brand, weight=1.0)
    elif delta.op == "less":
        profile.reinforce_disliked_brand(delta.anchor.brand, weight=1.0)


@observe(name="node.critique_apply", as_type="span")
async def critique_apply(state: WorkingState) -> dict:
    msg = state.message
    sess = get_store().get_or_create(state.chat_id)
    breadcrumbs: list[str] = []
    delta: CritiqueDelta | None = None

    # ── Callback path ──────────────────────────────────────────────────────
    if msg.callback_data and msg.callback_data.startswith("crit:click:"):
        # SPEC-IMPLICIT-FB-001 / REQ-FB-CLICK-001 — implicit click branch.
        # Distinct from crit:more/less/cheap: silent ack, no critique state mutation,
        # no further graph routing.
        from app.channels.implicit_feedback import (
            _brand_of,
            _keywords_for_product,
            record_click,
            resolve_click_target,
        )

        suffix = msg.callback_data[len("crit:click:") :]
        target = resolve_click_target(suffix, sess.last_results or [])
        if target is None:
            _last_ids = [str(getattr(c, "id", ""))[:36] for c in (sess.last_results or [])]
            logger.info(
                "[IMPLICIT_FB][stale-click] suffix=%s last_results_n=%d ids=%s",
                suffix[:53],
                len(sess.last_results or []),
                _last_ids,
            )
            await record_click(state.chat_id, sess.from_user_id, suffix or "", "", [], stale=True)
            stale_ack_done = False
            try:
                from app.graphs.nodes._adapter_ctx import get_adapter

                adapter = get_adapter()
                if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                    await adapter.answer_callback_query(msg.callback_query_id, "")
                    stale_ack_done = True
            except Exception:
                logger.debug("[critique_apply] click stale ack best-effort")
            breadcrumbs.append(f"critique_apply: stale-click ack={stale_ack_done}")
            return {"log_events": breadcrumbs, "sent_count": 0}

        brand = _brand_of(target)
        keywords = _keywords_for_product(target)
        product_id = str(getattr(target, "id", "") or "")
        try:
            await record_click(state.chat_id, sess.from_user_id, product_id, brand, keywords)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[critique_apply] record_click failed: %r", exc)
        # Silent ack
        try:
            from app.graphs.nodes._adapter_ctx import get_adapter

            adapter = get_adapter()
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, "")
        except Exception:
            logger.debug("[critique_apply] click silent ack best-effort")
        breadcrumbs.append(f"critique_apply: click product_id={product_id[:32]}")
        return {"log_events": breadcrumbs, "sent_count": 0}

    if msg.callback_data and msg.callback_data.startswith("crit:"):
        try:
            delta = parse_callback(msg.callback_data, sess.last_results)
        except Exception as exc:  # REQ-AGENT-007
            logger.exception("[critique_apply] parse_callback raised")
            breadcrumbs.append(f"critique_apply_error: {type(exc).__name__}"[:200])
            return {"log_events": breadcrumbs}

        from app.channels.lang import session_lang as _sess_lang

        _lang = _sess_lang(sess)

        if delta is None:
            # Stale/invalid callback — let routing send to respond.
            try:
                from app.graphs.nodes._adapter_ctx import get_adapter

                adapter = get_adapter()
                if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                    stale_msg = (
                        "오래된 카드예요 — 새로 검색해주세요" if _lang == "ko" else "Out of date — try a fresh search"
                    )
                    await adapter.answer_callback_query(msg.callback_query_id, stale_msg)
            except Exception:
                logger.debug("[critique_apply] answer_callback_query best-effort")
            breadcrumbs.append("critique_apply: stale callback")
            return {"log_events": breadcrumbs}

        # Toast acknowledgement (lang-aware)
        try:
            from app.graphs.nodes._adapter_ctx import get_adapter

            adapter = get_adapter()
            if _lang == "ko":
                toasts = {"more": "비슷한 거 더 찾는 중 ✨", "less": "다른 느낌으로 ✕", "cheap": "더 저렴한 걸로 💰"}
            else:
                toasts = {"more": "Finding more like this ✨", "less": "Steering away ✕", "cheap": "Going cheaper 💰"}
            toast = toasts.get(delta.op, "넵" if _lang == "ko" else "Got it")
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, toast)
        except Exception:
            logger.debug("[critique_apply] toast best-effort")

        # Reinforce taste
        if settings.TASTE_PROFILE_ENABLED:
            try:
                taste_store = get_taste_store()
                profile = taste_store.get_or_create(user_key_for(sess.from_user_id, sess.chat_id))
                _reinforce_taste(sess, profile, delta)
                taste_store.update(profile)
            except Exception:
                logger.exception("[critique_apply] taste reinforcement failed")

    # ── Text path (intent=critique_text from router OR AWAITING_INTENT) ────
    else:
        text = (msg.text or "").strip()
        if not text:
            return {"log_events": ["critique_apply: empty text"]}

        # Prefer router-provided structured delta; else build a free-text one.
        if state.decision is not None and state.decision.intent == RoutedIntent.CRITIQUE_TEXT:
            delta = state.decision.critique_delta or CritiqueDelta(op="free_text", extra_intent=text[:200])
        else:
            # AWAITING_INTENT path (raw user intent reply)
            delta = CritiqueDelta(op="free_text", extra_intent=text[:200])

        # Persist the user_intent on the session so search_node can reference it.
        sess.user_intent = text[:_MAX_INTENT_LEN]
        sess.state = SessionState.SEARCHING
        # Reset shown_product_ids on AWAITING_INTENT (fresh-intent search).
        if state.decision is None:
            sess.shown_product_ids = []
        get_store().update(sess)

    if delta is None:
        return {"log_events": ["critique_apply: no delta"]}

    summary = format_delta_summary(delta)
    breadcrumbs.append(f"critique_apply: op={delta.op} summary={summary[:80]}")
    return {
        "critique_delta": delta,
        "presearch_summary": summary,
        "messages": [SystemMessage(content=f"critique: {summary}")],
        "log_events": breadcrumbs,
    }
