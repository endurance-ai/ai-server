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
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.nodes._first_touch import maybe_first_touch
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


# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def _emit_user_input(state: WorkingState) -> None:
    """LOG-T0 (webhook intake) — emit the user's INBOUND event so a turn is
    reconstructable from its trigger. Exactly one of `user_callback` /
    `user_photo` / `user_text` by inbound shape (catalog #1–#3). Runs inside the
    `node.ingest` Langfuse span, so `emit()` auto-attaches the per-turn
    `langfuse_trace` (the reconstruction key). turn_no=0 — intake precedes the
    ingest node stage (=1).

    These three event types were documented in `event_payloads.py` as "webhook
    intake" but never actually emitted, leaving conversation-log turns with no
    visible trigger (agent tool-selection could not be aligned to what the user
    said) and starving `get_recent_history` of prior user phrasing. The raw text
    mirrors what `chat_service` already persists to the messages table and what
    `get_recent_history._summarize_payload` expects (capped 500). Never raises.
    """
    try:
        msg = state.message
        if msg is None:
            return
        common = {
            "user_key": user_key_for(state.from_user_id, state.chat_id),
            "chat_id": state.chat_id,
            "thread_id": state.thread_id,
            "turn_no": 0,
        }
        cb = msg.callback_data or ""
        if cb:
            emit(event_type="user_callback", payload={"callback_data": cb[:128]}, **common)
        elif msg.photo_file_id or msg.urls:
            emit(
                event_type="user_photo",
                payload={
                    "attachment_id": msg.photo_file_id or None,
                    "image_url": (str(msg.urls[0]) if msg.urls else None),
                    "caption": (msg.text or None),
                },
                **common,
            )
        elif msg.text:
            from app.channels.lang import detect_lang

            emit(
                event_type="user_text",
                payload={"text": msg.text[:500], "lang_detected": detect_lang(msg.text)},
                **common,
            )
    except Exception:  # noqa: BLE001 — observability must never block the webhook
        logger.debug("[ingest] user-input emit best-effort")


async def _send_callback_toast(state: WorkingState, sess, *, text: str | None) -> None:
    """Pop a tiny toast above the chat in response to an inline-button tap.

    Without this the user only sees the client's faint button-press flash and
    assumes the button is broken. Lang-aware default copy when `text` is None.
    Best-effort: any failure is swallowed (we never block the webhook on UX).
    """
    cb_id = getattr(state.message, "callback_query_id", None) if state.message else None
    if not cb_id:
        return
    try:
        from app.channels.lang import session_lang
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
        if adapter is None or not hasattr(adapter, "answer_callback_query"):
            return
        if text is None:
            lang = session_lang(sess)
            text = "❤️ 취향에 반영했어!" if lang == "ko" else "❤️ Got it — tuning your taste!"
        await adapter.answer_callback_query(cb_id, text=text)
    except Exception as exc:  # noqa: BLE001 — UX-only, must never raise
        logger.debug("[ingest] answer_callback_query best-effort failed: %r", exc)


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
        logger.info("❤️  [card:like] unresolved suffix=%r last_results=%d", suffix, len(last_results))
        await _send_callback_toast(state, sess, text=None)
        return

    product_id = str(_attr(target, "id", "") or "")
    brand = _brand_of(target)
    keywords = _keywords_for_product(target)
    logger.info(
        "❤️  [card:like] resolved product_id=%s brand=%r keywords=%s",
        product_id,
        brand,
        keywords[:5] if keywords else [],
    )
    await record_click(state.chat_id, sess.from_user_id, product_id, brand, keywords)
    breadcrumbs.append("ingest: card:like → record_click")
    logger.info("❤️  [card:like] record_click ok product_id=%s", product_id)
    # User-visible toast — without this the button looks dead.
    await _send_callback_toast(state, sess, text=None)

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


async def _handle_cap_membership_interest(state: WorkingState, sess, breadcrumbs: list[str]) -> None:
    """SPEC-DAILY-TOKEN-CAP-001 fake-door (260610) — membership upsell tap.

    Replies with a friendly "준비 중" message (KO/EN) plus a callback toast.
    The tap itself is the WTP signal — captured upstream as `user_callback`
    with `callback_data="cap:membership_interest"`. Best-effort; never raises
    into the webhook.
    """
    from app.channels.lang import session_lang
    from app.graphs.nodes._adapter_ctx import get_adapter

    lang = session_lang(sess)
    if lang == "ko":
        reply = "고마워! 🐱 멤버십 페이지는 아직 준비 중이야 — 출시되면 제일 먼저 알려줄게!"
        toast = "관심 받았어 🐱"
    else:
        reply = (
            "Thanks for the interest! 🐱 The membership page is still in the works "
            "— I'll ping you the moment it's live!"
        )
        toast = "Got it 🐱"
    try:
        adapter = get_adapter()
        if adapter is not None and hasattr(adapter, "send_text"):
            await adapter.send_text(state.chat_id, reply)
        breadcrumbs.append("ingest: cap:membership_interest reply sent")
    except Exception as exc:  # noqa: BLE001 — UX-only
        logger.debug("[ingest] cap:membership_interest reply failed: %r", exc)
    await _send_callback_toast(state, sess, text=toast)


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
            "이게 마지막이야 🐱 다른 스타일로 찾아볼까? 원하는 거 알려줘!"
            if lang == "ko"
            else "That's the last of them 🐱 Want me to look for a different style? Just tell me!"
        )
        try:
            await adapter.send_text(state.chat_id, msg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ingest] cards:more empty-notice send failed: %r", exc)


async def _handle_lang_pick(state: WorkingState, sess, lang: str, breadcrumbs: list[str]) -> None:
    """SPEC-ONBOARD-LITE-001 — `onboard:lang:{ko|en}` callback.

    Pins `sess.lang` to the user's choice, then sends the short service intro
    in that language. Deterministic terminal — does NOT route back through the
    agent. Best-effort throughout; never raises into the webhook.
    """
    from app.graphs.nodes.intro import INTRO_EN, INTRO_KO

    chosen = lang.strip().lower()
    if chosen not in ("ko", "en"):
        return

    # 1) Pin language to session (sticky for subsequent turns).
    try:
        sess.lang = chosen
        get_store().update(sess)
        breadcrumbs.append(f"ingest: lang pinned={chosen}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ingest] lang pin failed: %r", exc)

    # 2) Toast acknowledging the tap.
    _ack = "✅ 좋아!" if chosen == "ko" else "✅ Got it!"
    await _send_callback_toast(state, sess, text=_ack)

    # 3) Send the short service intro in the chosen language.
    try:
        adapter = get_adapter()
        text = INTRO_KO if chosen == "ko" else INTRO_EN
        await adapter.send_text(state.chat_id, text)
        breadcrumbs.append(f"ingest: intro sent lang={chosen}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ingest] intro send after lang pick failed: %r", exc)


async def _handle_gender_pick(state: WorkingState, sess, gender: str, breadcrumbs: list[str]) -> None:
    """SPEC-GENDER-PIN-001 — `clarify:gender:{men|women|unisex}` callback.

    Pins the chosen gender to the user's taste profile (cross-session), then
    pops the pending search args stashed by `search_products` and re-runs the
    search WITH the gender applied, delivering the hybrid album inline (same
    sender the post-search reply uses). Deterministic — does NOT route back
    through the agent. Best-effort throughout; never raises into the webhook.
    """
    from app.channels.lang import session_lang

    g = gender.strip().lower()
    if g not in ("men", "women", "unisex"):
        return
    # 1) Pin to taste profile (persisted).
    try:
        from app.infrastructure.memory.taste_profile import get_taste_store, user_key_for

        store = get_taste_store()
        profile = store.get_or_create(user_key_for(state.from_user_id, state.chat_id))
        profile.gender = g
        store.update(profile)
        breadcrumbs.append(f"ingest: gender pinned={g}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ingest] gender pin failed: %r", exc)

    # Toast acknowledging the tap.
    _ack = "✅ 기억해둘게!" if session_lang(sess) == "ko" else "✅ Got it!"
    await _send_callback_toast(state, sess, text=_ack)

    # 2) Pop the pending search and re-run with gender applied.
    from app.agents import pending_gender

    pending = pending_gender.pop_pending(state.chat_id)
    if not pending:
        # No pending search (e.g. user tapped a stale card) — just confirm.
        try:
            from app.graphs.nodes._adapter_ctx import get_adapter

            adapter = get_adapter()
            if session_lang(sess) == "ko":
                msg = "좋아, 이제 뭐 찾아줄까? 🐱"
            else:
                msg = "Great — what should I find for you? 🐱"
            await adapter.send_text(state.chat_id, msg)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[ingest] gender pick no-pending notice failed: %r", exc)
        return

    base_query = str(pending.get("text_query") or "").strip()
    text_query = f"{base_query} {g}".strip() if base_query else g
    try:
        from app.agents.tools.respond import send_hybrid_batch
        from app.agents.tools.search_products import persist_last_results, run_text_only_search
        from app.graphs.nodes._adapter_ctx import get_adapter

        cands = await run_text_only_search(
            text_query=text_query,
            category=pending.get("category"),
            # 2026-07-16 — v6 p_gender 하드 필터: 카드에서 고른 성별 그대로.
            # 'unisex' 는 search_service._resolve_gender 가 None(필터 off) 매핑.
            gender=g,
            top_k=int(pending.get("top_k") or 15),
            style_node_primary=getattr(state, "vision_outfit_style_node_primary", None),
            user_key=user_key_for(state.from_user_id, state.chat_id),
        )
        # Persist to session so send_hybrid_batch (which reads sess.last_results)
        # and the pager cursor work exactly like the post-search reply path.
        persist_last_results({"chat_id": state.chat_id}, cands)
        # Cross-turn: store the gender-pinned query so a follow-up refine
        # ("더 저렴하게") reuses it instead of the raw refine instruction.
        try:
            from app.agents.last_query import set_last_query

            set_last_query(state.chat_id, text_query)
        except Exception:  # noqa: BLE001
            pass
        # 260611 — emit `search_done` so the next turn's memory context shows
        # this inline-driven search (it doesn't go through the agent's tool
        # dispatcher so the upstream emit sites don't fire).
        try:
            from app.agents.tools.search_products import emit_search_done

            emit_search_done(
                chat_id=state.chat_id,
                user_key=user_key_for(state.from_user_id, state.chat_id),
                thread_id=state.thread_id,
                text_query=text_query,
                cands=cands,
                category=pending.get("category"),
                is_refine=False,
            )
        except Exception:  # noqa: BLE001 — observability is best-effort
            logger.debug("[gender_pick] search_done emit best-effort skip", exc_info=True)
        adapter = get_adapter()
        delivered = await send_hybrid_batch(adapter, state.chat_id, ctx=None, offset=0)
        breadcrumbs.append(f"ingest: gender resume search='{text_query}' delivered={delivered}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ingest] gender resume search failed: %r", exc)


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

    # LOG-T0 — record the inbound user event (user_text/photo/callback) at the
    # universal entry node so EVERY channel/path is covered in one place and the
    # emit lands inside this turn's Langfuse trace. Unlike the structural-only
    # breadcrumbs above, this row carries the raw text (own DB sink, not the
    # Langfuse metadata the breadcrumb comment guards) so tool-selection can be
    # aligned to the trigger.
    _emit_user_input(state)

    # P1-4 (260521 V3 eval): clear pending_question whenever the inbound is
    # NOT a plain-text answer. The 2026-05-20 pending-question carry-across-
    # turn mechanism (app/agents/pending_question.py) was designed to splice
    # short affirmative replies ("어"/"yes") back into the original intent.
    # But it kept firing on image/URL/callback turns too — the prior turn's
    # clarification question would be re-emitted as the bot's "response" to a
    # brand-new photo, which is what S3 of the V3 eval surfaced. A photo /
    # URL / callback is never an answer to a text question; clear pending.
    try:
        from app.agents.pending_question import clear_pending

        is_text_answer = bool(msg.text) and not msg.photo_file_id and not msg.urls and not msg.callback_data
        if not is_text_answer:
            clear_pending(state.chat_id)
    except Exception as exc:  # noqa: BLE001 — UX-only, never raise
        logger.debug("[ingest] pending_question clear best-effort failed: %r", exc)

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
        # SPEC-GENDER-PIN-001 — gender clarify is special: it pins to the taste
        # profile + re-runs the pending search inline (NOT boost_keywords).
        # Handled before the generic clarify path below.
        if cb_data.startswith("onboard:lang:"):
            # SPEC-ONBOARD-LITE-001 — language picker tap (new-user first-touch).
            lang_val = cb_data.split(":", 2)[2] if len(cb_data.split(":", 2)) >= 3 else ""
            await _handle_lang_pick(state, sess, lang_val, breadcrumbs)
        elif cb_data.startswith("clarify:gender:"):
            gender_val = cb_data.split(":", 2)[2] if len(cb_data.split(":", 2)) >= 3 else ""
            await _handle_gender_pick(state, sess, gender_val, breadcrumbs)
        elif cb_data.startswith("clarify:"):
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
        elif cb_data == "cap:membership_interest":
            # SPEC-DAILY-TOKEN-CAP-001 fake-door (260610) — membership upsell
            # tap. Inline absorb: send the "준비 중" reply + answer-callback
            # toast. The callback itself is captured as a `user_callback`
            # event (catalog #3) by webhook intake — SQL filter on
            # `payload->>'callback_data'='cap:membership_interest'` yields the
            # WTP click count.
            await _handle_cap_membership_interest(state, sess, breadcrumbs)
    except Exception as exc:  # noqa: BLE001 — never block webhook
        logger.debug("[ingest] hybrid card callback handling failed: %r", exc)

    # SPEC-ONBOARD-LITE-001 — first-touch greeting / /reset taste clear /
    # returning-/start ack. Inline side effects (same pattern as the
    # clarify/hybrid-card handlers above). Routing decision is in
    # _route_after_ingest_v2.
    try:
        await maybe_first_touch(state, sess, get_adapter(), breadcrumbs=breadcrumbs)
    except Exception as exc:  # noqa: BLE001 — never block webhook
        logger.debug("[ingest] first_touch handling failed: %r", exc)

    # SPEC-AGENT-V2-CLEANUP-001 — the ReAct agent topology is the only
    # topology, so ingest NEVER invokes the legacy LLM 4-way `route_text`
    # router (no routing closure / `agent` node / `react_loop` reads
    # `state.decision`). Return here after Step A (implicit feedback) +
    # Step C (clarify inline) side effects; `decision` stays unset.
    _emit_intent_routed(state)
    node_done("ingest", route="agent_skip_router", state=sess.state.value)
    return {"log_events": breadcrumbs, "turn_no": 1}
