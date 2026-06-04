"""SPEC-ONBOARD-LITE-001 — ingest-inline first-touch / reset / start-ack.

Side-effecting helper invoked from `ingest` (the node already runs inline
side effects for implicit-feedback / clarify / hybrid-card callbacks). Pure
routing stays in `_route_after_ingest_v2`.

Behavior matrix (gated on `sess.onboarded_at` + message shape):
  - `/reset` keyword (any user)        -> taste_delete(user_key) + ack
  - new user + actionable              -> 1-line greeting + mark onboarded_at
  - new user + `/start`-only           -> no-op (intro node handles + marks)
  - returning user + `/start`-only     -> light ready ack
  - returning user + actionable        -> no-op
`actionable` = photo OR urls OR (text and text.strip() != "/start").
contentless (no text/cb/url/photo) is never first-touch — handled by the
router's silent-END guard.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.channels.reset_keywords import is_reset_keyword
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import get_taste_store, user_key_for
from app.observability.conversation_log import emit

logger = logging.getLogger(__name__)

_GREET_KO = "안녕! 난 kiko야 🐱 바로 찾아볼게."
_GREET_EN = "Hey! I'm kiko 🐱 — finding it now."
_RESET_KO = "취향 기록 초기화했어 🐱 새로 시작해보자!"
_RESET_EN = "Cleared your taste history 🐱 fresh start!"
_READY_KO = "어 왔구나 🐱 뭐 찾아줄까?"
_READY_EN = "Hey, welcome back 🐱 what are we looking for?"


def _is_start_only(text: str | None) -> bool:
    return (text or "").strip().lower() == "/start"


def _is_actionable(msg: Any) -> bool:
    if msg.photo_file_id or msg.urls:
        return True
    t = (msg.text or "").strip()
    return bool(t) and t.lower() != "/start"


async def maybe_first_touch(
    state: Any,
    sess: Any,
    adapter: Any,
    *,
    taste_delete: Callable[[str], None] | None = None,
    breadcrumbs: list[str],
) -> None:
    """Run the first-touch / reset / ready-ack side effects. Never raises."""
    msg = state.message
    user_key = user_key_for(state.from_user_id, state.chat_id)
    lang = getattr(sess, "lang", "en")

    # 1. /reset -> clear taste profile + origin image + ack (any user).
    if is_reset_keyword(msg.text):
        try:
            (taste_delete or get_taste_store().delete)(user_key)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [first_touch] taste delete failed")
        # Also clear the stored origin image for multi-turn blending so the
        # user genuinely starts fresh (no carry-over of previous outfit).
        try:
            from app.agents.origin_image import clear_origin

            clear_origin(state.chat_id)
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] origin image clear best-effort", exc_info=True)
        try:
            await adapter.send_text(state.chat_id, _RESET_KO if lang == "ko" else _RESET_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] reset ack send best-effort", exc_info=True)
        breadcrumbs.append("first_touch: reset taste + origin cleared")
        return

    is_new = getattr(sess, "onboarded_at", None) is None

    # 2. new user + actionable -> greeting + mark onboarded_at (same turn proceeds).
    if is_new and _is_actionable(msg):
        try:
            await adapter.send_text(state.chat_id, _GREET_KO if lang == "ko" else _GREET_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] greeting send best-effort", exc_info=True)
        try:
            sess.onboarded_at = datetime.now(UTC)
            get_store().update(sess)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [first_touch] onboarded_at persist failed")
        try:
            emit(
                event_type="bot_text",
                user_key=user_key,
                chat_id=state.chat_id,
                thread_id=getattr(state, "thread_id", None),
                turn_no=getattr(state, "turn_no", 1),
                payload={"flow": "first_touch", "chunk_index": 0, "total_chunks": 1},
            )
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] bot_text emit best-effort", exc_info=True)
        breadcrumbs.append("first_touch: greeted + marked")
        return

    # 3. returning user + /start-only -> light ready ack.
    if not is_new and _is_start_only(msg.text) and not (msg.photo_file_id or msg.urls):
        try:
            await adapter.send_text(state.chat_id, _READY_KO if lang == "ko" else _READY_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] ready ack send best-effort", exc_info=True)
        breadcrumbs.append("first_touch: returning /start ack")
        return

    # new user + /start-only -> no-op (intro node); returning + actionable -> no-op.
    return
