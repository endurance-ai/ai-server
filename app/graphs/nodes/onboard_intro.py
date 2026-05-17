"""Onboarding Stage 0 — intro / restart confirmation.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-ENTRY-001 + REQ-ONBOARD-ENTRY-002 +
REQ-ONBOARD-LANG-002 + REQ-ONBOARD-OBS-001.

Two use cases (single node — branch on `sess.onboarded_at`):

(a) Fresh user (`onboarded_at IS NULL`) OR explicit re-trigger keyword (`/reset`,
    "온보딩 다시", "취향 다시 설정") OR returning user confirmed "yes" callback:
    Send 3-line greeting + 3-line usage guide as ONE text message, followed
    immediately by the Stage 1 mood card. Sets `state.onboard_stage = "mood"`.

(b) Returning user types `/start` (no re-trigger keyword, already onboarded):
    Send the "다시 시작할까요?" confirmation card only. State stays at
    `onboard_stage="intro"` until the user taps yes/no.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import logging
from typing import Any

from app.channels.factory import get_adapter
from app.channels.lang import session_lang
from app.channels.onboarding_cards import (
    build_mood_card,
    build_restart_confirmation_card,
)

# Restart keyword detection — canonical source (code review P0-2 fix:
# previously duplicated here with divergent keyword set vs routing.py).
from app.channels.onboarding_values import is_restart_keyword as _is_restart_keyword
from app.graphs.nodes._trace import node_done, node_enter
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.observability.langfuse import observe
from app.observability.langfuse import update_current_span as update_current_observation

logger = logging.getLogger(__name__)

# ─── intro chunks (sticky lang) ────────────────────────────────────────────
# 한 뭉텅이로 안 보내고 3~4 청크로 끊어서 보낸다 (대화감, 사용자 피드백 #1).
# 각 청크 사이에 짧은 typing + delay 가 들어가 인간 메신저 느낌으로 흐름.
_INTRO_CHUNKS_KO: list[str] = [
    "어 안녕! 🐱",
    "나는 kiko야. 너 취향에 맞는 옷 같이 찾아주는 친구.",
    "처음이지? 잠깐 너 취향 좀 알아볼게.\n사진 보내거나 핀터레스트/인스타 링크 던져도 돼. 말로 설명해도 OK.",
    "일단 무드부터 골라볼까? 👇",
]
_INTRO_CHUNKS_EN: list[str] = [
    "hey! 🐱",
    "I'm kiko — your fashion sidekick.",
    "first time? lemme get a feel for what you like.\n"
    "you can send photos, drop pinterest/insta links, or just text me.",
    "let's start with the vibe 👇",
]


def _is_restart_attempt(sess: Any, state: WorkingState) -> bool:
    """Returning user + `/start` text + NO re-trigger keyword + NO yes-callback.

    Returns True iff we should show the confirmation card instead of the full
    intro flow.
    """
    if getattr(sess, "onboarded_at", None) is None:
        return False
    # Yes-callback from a prior confirmation card → treat as confirmed restart,
    # NOT a fresh restart prompt.
    cb = state.message.callback_data or ""
    if cb == "onboard:restart:yes":
        return False
    text = (state.message.text or "").strip().lower()
    if _is_restart_keyword(text):
        return False  # explicit keyword bypasses confirmation
    return text == "/start"


# @MX:ANCHOR: [AUTO] Entry point for the entire onboarding flow.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
# @MX:REASON: REQ-ONBOARD-ENTRY-001/002 — gate for fresh users + restart UX.
#   Three downstream callers (routing, webhook intake retry, restart callback)
#   depend on the two-branch contract (intro vs confirmation).
@observe(name="onboarding.intro", as_type="span")
async def onboard_intro(state: WorkingState) -> dict:
    """Intro / restart confirmation node.

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    node_enter("onboard_intro")
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    adapter = get_adapter()

    is_restart = _is_restart_attempt(sess, state)
    try:
        update_current_observation(metadata={"lang": lang, "is_restart_attempt": is_restart})
    except Exception:  # noqa: BLE001
        pass

    if is_restart:
        # Returning user — confirmation card only. Do NOT mutate onboard_stage.
        text, kb = build_restart_confirmation_card(lang)
        try:
            await adapter.send_text_with_keyboard(state.chat_id, text, kb)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [ONBOARD] restart confirmation card send failed")
        # Mark the session as IN the intro hold so routing keeps subsequent
        # yes/no callbacks coming back here.
        sess.onboard_stage = "intro"
        get_store().update(sess)
        node_done("onboard_intro", branch="restart_confirmation", lang=lang)
        return {
            "onboard_stage": "intro",
            "log_events": [f"onboard_intro: restart_confirmation lang={lang}"],
        }

    # Fresh user OR explicit re-trigger OR confirmed yes-callback path.
    # 인트로를 3~4 청크로 끊어서 보낸다 — 사용자 피드백 #1 (한 뭉텅이는 짜침).
    # 각 청크 직전에 typing action + 짧은 딜레이를 둬서 메신저 톤 살림.
    import asyncio as _asyncio

    intro_chunks = _INTRO_CHUNKS_KO if lang == "ko" else _INTRO_CHUNKS_EN
    intro_delay_s = 0.6
    for chunk in intro_chunks:
        try:
            if hasattr(adapter, "send_chat_action"):
                await adapter.send_chat_action(state.chat_id, "typing")
        except Exception:  # noqa: BLE001 — best-effort typing indicator
            pass
        try:
            await _asyncio.sleep(intro_delay_s)
            await adapter.send_text(state.chat_id, chunk)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [ONBOARD] intro chunk send failed")

    # Stage 1 mood card.
    text, kb = build_mood_card(lang, selections=[])
    msg_id: int | None = None
    try:
        msg_id = await adapter.send_text_with_keyboard(state.chat_id, text, kb)
    except Exception:  # noqa: BLE001
        logger.exception("🐱 [ONBOARD] mood card send failed")

    # Mutate session — onboard_stage advances to "mood", selections reset
    # (re-onboarding starts fresh; merging is at completion via additive seed).
    sess.onboard_stage = "mood"
    sess.onboard_selections = {"mood": [], "color": [], "fit": []}
    sess.onboard_card_message_id = msg_id
    # Re-onboarding: keep `onboarded_at` until the next `complete_onboarding`
    # rewrites it (additive merge contract per REQ-ONBOARD-SEED-001).
    get_store().update(sess)

    node_done("onboard_intro", branch="stage1_sent", lang=lang)
    return {
        "onboard_stage": "mood",
        "onboard_selections": {"mood": [], "color": [], "fit": []},
        "onboard_card_message_id": msg_id,
        "log_events": [f"onboard_intro: stage1_sent lang={lang}"],
    }


__all__ = ["onboard_intro", "_is_restart_keyword"]
