"""SPEC-ONBOARD-LITE-001 -- lightweight first-touch language picker.

Reached only when a brand-new user (`sess.onboarded_at IS NULL`) sends a
`/start`-only first message (the `_route_after_ingest_v2` gate in
`fashion_bot.py`). The onboarding card subgraph was removed; an actionable
first message (photo / link / style text) is greeted inline by `ingest`
(`maybe_first_touch`) and proceeds to a recommendation the same turn -- it
never reaches this node.

This node sends a single bilingual language-picker card (KR / EN buttons),
marks the user as introduced (`sess.onboarded_at = now()`, persisted via the
session store), and terminates the turn. The picker callback
(`onboard:lang:{ko|en}`) is consumed inline by `ingest._handle_lang_pick`
which pins `sess.lang` and sends the short service intro in the chosen
language. Gender pinning was removed from onboarding — it is collected
lazily on the first text search if missing (SPEC-GENDER-PIN-001).

Setting `onboarded_at` at picker-send time (not at picker-pick time) is
deliberate: if the user ignores the card and sends a real query instead,
they fall through to the normal flow with auto-detected language rather
than being trapped in an intro loop.

@MX:SPEC: SPEC-ONBOARD-LITE-001
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.channels.factory import get_adapter
from app.graphs.nodes._trace import node_done, node_enter
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

_LANG_PICKER_TEXT = "\U0001f44b Hi! 한국어 / English"
_LANG_PICKER_BUTTONS: list[list[tuple[str, str]]] = [
    [
        ("\U0001f1f0\U0001f1f7 한국어", "onboard:lang:ko"),
        ("\U0001f1fa\U0001f1f8 English", "onboard:lang:en"),
    ],
]

# Short service intro fired after the user picks a language. Single source
# of truth — imported by `ingest._handle_lang_pick`.
INTRO_KO = (
    "안녕! 나 kiko야 \U0001f431\n"
    "취향에 맞는 옷 같이 찾아주는 패션 친구야.\n"
    "\n"
    "이미지 링크 / 핀터레스트 링크 / 글로 설명 - 다 OK ✨\n"
    '예: "와이드 청바지 찾아줘"\n'
    "\n"
    "자, 첫 요청 보내줘!"
)
INTRO_EN = (
    "Hey, I'm kiko \U0001f431\n"
    "Your fashion sidekick — I find pieces that match your taste.\n"
    "\n"
    "Send me image links, Pinterest links, or just describe what you want ✨\n"
    'e.g. "find me a pair of wide-leg jeans"\n'
    "\n"
    "Go ahead, I'm ready!"
)


async def _send_lang_picker(chat_id: int, adapter) -> None:
    """Send the bilingual language-picker card. Best-effort — never raises."""
    if hasattr(adapter, "send_text_with_keyboard"):
        await adapter.send_text_with_keyboard(chat_id, _LANG_PICKER_TEXT, _LANG_PICKER_BUTTONS)
    elif hasattr(adapter, "send_text_with_buttons"):
        flat = [label for row in _LANG_PICKER_BUTTONS for label, _ in row]
        await adapter.send_text_with_buttons(chat_id, _LANG_PICKER_TEXT, flat)
    else:
        await adapter.send_text(chat_id, _LANG_PICKER_TEXT)


# @MX:ANCHOR: [AUTO] First-touch intro entry (onboarding-cards OFF path).
# @MX:SPEC: SPEC-AGENT-V2-REACT
# @MX:REASON: routing._route_after_ingest_v2 gates the entire new-user path
#   here; the onboarded_at persist contract (survives next webhook) is what
#   prevents an infinite intro loop.
@observe(name="node.intro", as_type="span")
async def intro(state: WorkingState) -> dict:
    """Send the language picker, mark introduced, terminate the turn."""
    node_enter("intro")
    sess = get_store().get_or_create(state.chat_id)

    adapter = get_adapter()
    try:
        await _send_lang_picker(state.chat_id, adapter)
    except Exception:  # noqa: BLE001
        logger.exception("\U0001f431 [intro] lang picker send failed")

    # Mark introduced + persist. Setting this BEFORE the user picks a language
    # is intentional: it prevents the intro from looping if the user ignores
    # the picker and sends a real query instead.
    try:
        sess.onboarded_at = datetime.now(UTC)
        get_store().update(sess)
    except Exception:  # noqa: BLE001
        logger.exception("\U0001f431 [intro] onboarded_at persist failed")

    try:
        emit(
            event_type="bot_text",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=state.turn_no,
            payload={"flow": "intro_lang_picker", "chunk_index": 0, "total_chunks": 1},
        )
    except Exception:  # noqa: BLE001
        logger.debug("\U0001f431 [intro] bot_text emit best-effort", exc_info=True)

    node_done("intro", sent="lang_picker")
    return {"log_events": ["intro: lang_picker_sent"]}


__all__ = ["intro", "INTRO_KO", "INTRO_EN"]
