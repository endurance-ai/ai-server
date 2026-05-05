"""SPEC-AGENT-001 / REQ-AGENT-004 (node 1/10) — ingest.

Normalizes the inbound message into WorkingState. For text in RESULTS_SENT or
IDLE, invokes `app.channels.router.route_text` to write `decision`. The actual
branching off ingest is in `routing._route_after_ingest`.

Wraps: `app/channels/router.py::route_text` (text branch only).
"""

from __future__ import annotations

import logging

from app.channels.router import RoutedDecision, RoutedIntent, route_text
from app.channels.session import SessionState, get_store
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)


async def ingest(state: WorkingState) -> dict:
    msg = state.message
    sess = get_store().get_or_create(state.chat_id)
    if msg.from_user_id and not sess.from_user_id:
        sess.from_user_id = msg.from_user_id
        get_store().update(sess)

    breadcrumbs: list[str] = [
        f"ingest: state={sess.state.value} text={(msg.text or '')[:40]!r} "
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
        breadcrumbs.append(f"ingest_error: {type(exc).__name__}: {exc}"[:200])
        # Soft fallback so the graph can still terminate at respond.
        return {
            "decision": RoutedDecision(intent=RoutedIntent.OFF_TOPIC),
            "log_events": breadcrumbs,
        }

    breadcrumbs.append(f"ingest_router: intent={decision.intent.value}")
    return {"decision": decision, "log_events": breadcrumbs}
