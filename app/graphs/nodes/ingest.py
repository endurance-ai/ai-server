"""SPEC-AGENT-001 / REQ-AGENT-004 (node 1/10) — ingest.

Normalizes the inbound message into WorkingState. SPEC-AGENT-V2-CLEANUP-001:
the ReAct agent topology is now the only topology, so ingest never invokes the
legacy LLM 4-way `route_text` router — it returns early after the implicit
feedback + clarify-inline steps. The actual branching off ingest is in the
inline `_route_after_ingest_v2` closure in `fashion_bot.py`.
"""

from __future__ import annotations

import logging

from app.channels.lang import remember_lang
from app.graphs.nodes._trace import node_done, node_enter
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def _emit_intent_routed(state: WorkingState) -> None:
    """LOG-T11 — emit `intent_routed` at the success terminus of `ingest`.

    SPEC-AGENT-V2-CLEANUP-001 — the legacy LLM router was removed, so `ingest`
    never produces a `decision`; the intent label is always "unknown". Never
    raises.
    """
    try:
        emit(
            event_type="intent_routed",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=1,
            payload={
                "intent": "unknown",
                "critique_delta_summary": None,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ingest] intent_routed emit best-effort")


async def _handle_card_like(state: WorkingState, sess, cb_data: str, breadcrumbs: list[str]) -> None:
    """`card:like:{product_id_or_idx}` → positive taste signal.

    Resolves the tapped product against `sess.last_results` (the same field
    the per-card crit:click path consumes), then routes through the EXISTING
    `implicit_feedback.record_click` — the canonical taste-reinforcement entry
    point (reinforces liked_brand + liked_keywords and marks the impression
    clicked). Same downstream effect as the per-card crit:click button. Also
    emits the `card_clicked` conversation-log event (catalog #14).
    """
    from app.channels._candidate_attr import attr as _attr
    from app.channels.implicit_feedback import (
        _brand_of,
        _keywords_for_product,
        record_click,
        resolve_click_target,
    )

    suffix = cb_data[len("card:like:") :]
    last_results = list(getattr(sess, "last_results", None) or [])
    target = resolve_click_target(suffix, last_results)
    if target is None and suffix.isdigit():
        idx = int(suffix)
        if 0 <= idx < len(last_results):
            target = last_results[idx]
    if target is None:
        breadcrumbs.append("ingest: card:like unresolved")
        return

    product_id = str(_attr(target, "id", "") or "")
    brand = _brand_of(target)
    keywords = _keywords_for_product(target)
    await record_click(state.chat_id, sess.from_user_id, product_id, brand, keywords)
    breadcrumbs.append("ingest: card:like → record_click")

    try:
        position: int | None = None
        for i, c in enumerate(last_results):
            if str(_attr(c, "id", "") or "") == product_id and product_id:
                position = i
                break
        emit(
            event_type="card_clicked",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=1,
            payload={"product_id": product_id, "position": position, "dwell_ms": None},
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ingest] card_clicked emit best-effort")


async def _handle_cards_more(state: WorkingState, sess, breadcrumbs: list[str]) -> None:
    """`cards:more` → deliver the NEXT album+summary batch from
    `sess.last_results`, reusing the exact hybrid sender the post-search reply
    uses (`respond.send_hybrid_batch`). The in-memory pager cursor advances
    automatically. No `ctx` is passed (fresh webhook → no per-turn idempotency
    needed; the cursor itself prevents re-sending the same slice)."""
    try:
        from app.agents.tools.respond import send_hybrid_batch
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ingest] cards:more adapter/import failed: %r", exc)
        return
    delivered = await send_hybrid_batch(adapter, state.chat_id, ctx=None, offset=None)
    breadcrumbs.append(f"ingest: cards:more delivered={delivered}")
    if delivered == 0:
        # Nothing left to page (the "더보기" button is normally suppressed when
        # there is no next batch, but a stale tap can still arrive). Don't go
        # silent — nudge toward a fresh/refined search.
        try:
            from app.channels.lang import session_lang

            lang = session_lang(sess)
        except Exception:  # noqa: BLE001
            lang = "en"
        msg = (
            "이게 마지막이에요 🐱 다른 스타일로 찾아볼까요? 원하는 걸 알려주세요!"
            if lang == "ko"
            else "That's the last of them 🐱 Want me to look for a different style? Just tell me!"
        )
        try:
            await adapter.send_text(state.chat_id, msg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ingest] cards:more empty-notice send failed: %r", exc)


@observe(name="node.ingest", as_type="span")
async def ingest(state: WorkingState) -> dict:
    node_enter("ingest")
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
        ).startswith(("crit:", "clarify:", "card:", "cards:"))
        await _ifb.detect_and_apply_re_query(sess, inbound_is_fresh_query)
    except Exception as exc:  # noqa: BLE001 — never block webhook
        logger.warning("[ingest] implicit feedback steps failed: %r", exc)

    # SPEC-AGENT-V2-CLEANUP-001 — inline clarify:* callback handling. For
    # onboarded users, accumulate boost_keywords directly into the session so
    # the agent can use them on the next iteration. Mid-onboarding clarify is
    # ignored + node_error logged.
    try:
        cb_data = msg.callback_data or ""
        if cb_data.startswith("clarify:"):
            if getattr(sess, "onboarded_at", None) is None:
                breadcrumbs.append("ingest_v2: clarify mid-onboarding ignored")
                emit(
                    event_type="node_error",
                    user_key=user_key_for(state.from_user_id, state.chat_id),
                    chat_id=state.chat_id,
                    thread_id=state.thread_id,
                    turn_no=1,
                    payload={
                        "node_name": "ingest",
                        "exception_type": "ClarifyMidOnboarding",
                        "message": "clarify callback during onboarding — ignored",
                        "recovered": True,
                    },
                )
            else:
                # Parse `clarify:{axis}:{value}` → append value to boost_keywords.
                parts = cb_data.split(":", 2)
                if len(parts) >= 3 and parts[2]:
                    value = parts[2][:64]
                    cur = list(getattr(sess, "boost_keywords", []) or [])
                    if value not in cur:
                        cur.append(value)
                        try:
                            setattr(sess, "boost_keywords", cur)
                            get_store().update(sess)
                        except Exception:  # noqa: BLE001
                            pass
                breadcrumbs.append("ingest_v2: clarify boost_keywords accumulated")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ingest] v2 clarify inline handling failed: %r", exc)

    # Hybrid result-card callbacks (album+summary delivery UX). Handled here
    # in the same callback section as clarify:* so they are NOT treated as a
    # fresh text query (already excluded from `inbound_is_fresh_query` above).
    #   - card:like:{pid|idx} → positive taste signal for that product. Reuses
    #     the EXISTING click plumbing (`record_click`) so the downstream effect
    #     is identical to the per-card crit:click path; emits `card_clicked`.
    #   - cards:more → send the NEXT album+summary batch from sess.last_results.
    #   - cards:refine → no side effect here; flows to the agent (which already
    #     exposes refine_search / suggest_next_step) — no new search invented.
    # crit:* / clarify:* behavior is untouched.
    try:
        cb_data = msg.callback_data or ""
        if cb_data.startswith("card:like:"):
            await _handle_card_like(state, sess, cb_data, breadcrumbs)
        elif cb_data == "cards:more":
            await _handle_cards_more(state, sess, breadcrumbs)
    except Exception as exc:  # noqa: BLE001 — never block webhook
        logger.debug("[ingest] hybrid card callback handling failed: %r", exc)

    # SPEC-AGENT-V2-CLEANUP-001 — the ReAct agent topology is the only
    # topology, so ingest NEVER invokes the legacy LLM 4-way `route_text`
    # router (no routing closure / `agent` node / `react_loop` reads
    # `state.decision`). Return here after Step A (implicit feedback) +
    # Step C (clarify inline) side effects; `decision` stays unset.
    _emit_intent_routed(state)
    node_done("ingest", route="agent_skip_router", state=sess.state.value)
    return {"log_events": breadcrumbs, "turn_no": 1}
