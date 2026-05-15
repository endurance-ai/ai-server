"""SPEC-AGENT-001 / REQ-AGENT-004 (node 1/10) — ingest.

Normalizes the inbound message into WorkingState. For text in RESULTS_SENT or
IDLE, invokes `app.channels.router.route_text` to write `decision`. The actual
branching off ingest is in `routing._route_after_ingest`.

Wraps: `app/channels/router.py::route_text` (text branch only).
"""

from __future__ import annotations

import logging

from app.channels.lang import remember_lang
from app.channels.router import RoutedDecision, RoutedIntent, route_text
from app.channels.session import SessionState, get_store
from app.channels.taste_profile import user_key_for
from app.graphs.state import WorkingState
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def _emit_intent_routed(state: WorkingState, decision: RoutedDecision | None) -> None:
    """LOG-T11 — emit `intent_routed` at the success terminus of `ingest`.

    Never raises. Captures intent label + optional critique_delta summary
    (None when decision is missing or has no critique delta).
    """
    try:
        intent = decision.intent.value if decision is not None else "unknown"
        critique_summary: str | None = None
        if decision is not None and getattr(decision, "critique_delta", None) is not None:
            cd = decision.critique_delta
            critique_summary = f"op={cd.op}"
        emit(
            event_type="intent_routed",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=1,
            payload={
                "intent": intent,
                "critique_delta_summary": critique_summary,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ingest] intent_routed emit best-effort")


@observe(name="node.ingest", as_type="span")
async def ingest(state: WorkingState) -> dict:
    msg = state.message
    sess = get_store().get_or_create(state.chat_id)
    # Sticky language detection — once user types Korean, subsequent button
    # callbacks (no text) keep replying in Korean.
    prev_lang = getattr(sess, "lang", "en")
    new_lang = remember_lang(sess, msg.text)
    lang_changed = new_lang != prev_lang
    fuid_set = msg.from_user_id and not sess.from_user_id
    if fuid_set:
        sess.from_user_id = msg.from_user_id
    # Single unconditional update covers BOTH from_user_id assignment AND
    # lang change. Previously the elif swallowed lang_changed when both
    # conditions were true on a brand-new user's first turn (review P1).
    if fuid_set or lang_changed:
        get_store().update(sess)

    # Avoid logging raw user text — breadcrumbs flow into Langfuse trace
    # metadata. Record only structural shape so traces remain useful for
    # debugging without leaking user content.
    breadcrumbs: list[str] = [
        f"ingest: state={sess.state.value} text_len={len(msg.text or '')} "
        f"photo={bool(msg.photo_file_id)} urls={len(msg.urls)} cb={msg.callback_data or '—'}"
    ]

    # SPEC-IMPLICIT-FB-001 / REQ-FB-NOCLICK-001 + REQ-FB-REQUERY-001 — lazy steps.
    # Run BEFORE state-mutating logic so re-query can read last_results.
    try:
        from app.channels import implicit_feedback as _ifb

        await _ifb.attribute_expired_impressions(state.chat_id, sess.from_user_id)
        inbound_is_fresh_query = bool((msg.text and not msg.callback_data) or msg.photo_file_id or msg.urls) and not (
            msg.callback_data or ""
        ).startswith(("crit:", "clarify:"))
        await _ifb.detect_and_apply_re_query(sess, inbound_is_fresh_query)
    except Exception as exc:  # noqa: BLE001 — never block webhook
        logger.warning("[ingest] implicit feedback steps failed: %r", exc)

    # Only invoke router for ambiguous text in RESULTS_SENT / IDLE.
    needs_router = (
        msg.text
        and not msg.photo_file_id
        and not msg.urls
        and not msg.callback_data
        and sess.state in (SessionState.RESULTS_SENT, SessionState.IDLE, SessionState.AWAITING_INTENT)
    )
    if not needs_router:
        # LOG-T11 — emit even when router was skipped (decision stays None).
        _emit_intent_routed(state, None)
        return {"log_events": breadcrumbs, "turn_no": 1}

    try:
        decision: RoutedDecision = await route_text(msg.text or "", sess.state, sess.last_results)
    except Exception as exc:  # REQ-AGENT-007 — never propagate
        logger.exception("[ingest] router.route_text raised")
        # Strip exception detail (may carry user text echoed by upstream LLM)
        breadcrumbs.append(f"ingest_error: {type(exc).__name__}")
        fallback_decision = RoutedDecision(intent=RoutedIntent.OFF_TOPIC)
        _emit_intent_routed(state, fallback_decision)
        # Soft fallback so the graph can still terminate at respond.
        return {
            "decision": fallback_decision,
            "log_events": breadcrumbs,
            "turn_no": 1,
        }

    breadcrumbs.append(f"ingest_router: intent={decision.intent.value}")
    _emit_intent_routed(state, decision)
    return {"decision": decision, "log_events": breadcrumbs, "turn_no": 1}
