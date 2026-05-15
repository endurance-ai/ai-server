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
from app.channels.session import get_store
from app.graphs.state import WorkingState
from app.observability.langfuse import observe
from app.observability.langfuse import update_current_span as update_current_observation

logger = logging.getLogger(__name__)


# Re-trigger keywords — exact-match (whitespace-trimmed, case-insensitive)
# per plan §7.2 decision: Python regex `\b` on Korean is unreliable.
_RESTART_KEYWORDS_LOWER = frozenset({"/reset", "온보딩 다시", "취향 다시 설정"})


def _is_restart_keyword(text: str | None) -> bool:
    """Plan §7.2 — exact-match (whitespace-trimmed, lowercased) restart trigger."""
    if not text:
        return False
    return text.strip().lower() in _RESTART_KEYWORDS_LOWER


# ─── intro lines (sticky lang) ────────────────────────────────────────────────
_INTRO_LINES_KO: list[str] = [
    "안녕하세요, kiko 예요. 🐱",
    "당신만의 패션 큐레이터로 함께할게요.",
    "처음이니까 취향부터 알아볼게요!",
    "📸 사진을 보내면 비슷한 옷을 찾아드려요.",
    "🔗 핀터레스트나 인스타 링크도 OK.",
    "💬 '오버핏 좋아해' 같은 자연어도 받아요.",
    "먼저 무드부터 골라볼까요? ↓",
]
_INTRO_LINES_EN: list[str] = [
    "Hi, I'm kiko. 🐱",
    "Your personal fashion curator.",
    "Since you're new, let's figure out your taste first!",
    "📸 Send me a photo, I'll find similar pieces.",
    "🔗 Pinterest / Instagram links also work.",
    "💬 You can also type — e.g. 'I like oversized'.",
    "Let's start with mood ↓",
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
        return {
            "onboard_stage": "intro",
            "log_events": [f"onboard_intro: restart_confirmation lang={lang}"],
        }

    # Fresh user OR explicit re-trigger OR confirmed yes-callback path.
    intro_lines = _INTRO_LINES_KO if lang == "ko" else _INTRO_LINES_EN
    try:
        await adapter.send_text(state.chat_id, "\n".join(intro_lines))
    except Exception:  # noqa: BLE001
        logger.exception("🐱 [ONBOARD] intro text send failed")

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

    return {
        "onboard_stage": "mood",
        "onboard_selections": {"mood": [], "color": [], "fit": []},
        "onboard_card_message_id": msg_id,
        "log_events": [f"onboard_intro: stage1_sent lang={lang}"],
    }


__all__ = ["onboard_intro", "_is_restart_keyword"]
