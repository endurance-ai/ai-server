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
from app.graphs.state import WorkingState
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


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

    # Only invoke router for ambiguous text in RESULTS_SENT / IDLE.
    needs_router = (
        msg.text
        and not msg.photo_file_id
        and not msg.urls
        and not msg.callback_data
        and sess.state in (SessionState.RESULTS_SENT, SessionState.IDLE)
    )
    if not needs_router:
        return {"log_events": breadcrumbs}

    try:
        decision: RoutedDecision = await route_text(msg.text or "", sess.state, sess.last_results)
    except Exception as exc:  # REQ-AGENT-007 — never propagate
        logger.exception("[ingest] router.route_text raised")
        # Strip exception detail (may carry user text echoed by upstream LLM)
        breadcrumbs.append(f"ingest_error: {type(exc).__name__}")
        # Soft fallback so the graph can still terminate at respond.
        return {
            "decision": RoutedDecision(intent=RoutedIntent.OFF_TOPIC),
            "log_events": breadcrumbs,
        }

    breadcrumbs.append(f"ingest_router: intent={decision.intent.value}")
    return {"decision": decision, "log_events": breadcrumbs}
