"""SPEC-ONBOARD-LITE-001 -- lightweight first-touch service intro.

Reached only when a brand-new user (`sess.onboarded_at IS NULL`) sends a
`/start`-only first message (the `_route_after_ingest_v2` gate in
`fashion_bot.py`). The onboarding card subgraph was removed; an actionable
first message (photo / link / style text) is greeted inline by `ingest`
(`maybe_first_touch`) and proceeds to a recommendation the same turn -- it
never reaches this node. This node sends ONE deterministic, friendly intro
message + a gender selection card, marks the user as introduced
(`sess.onboarded_at = now()`, persisted via the session store), and
terminates the turn. The gender card callback (`clarify:gender:*`) is
consumed inline by `ingest._handle_gender_pick` (SPEC-GENDER-PIN-001) which
pins the choice to `taste_profile.gender` -- no pending search exists at
onboarding so it simply confirms and prompts for the first request.

@MX:SPEC: SPEC-ONBOARD-LITE-001
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.channels.factory import get_adapter
from app.channels.lang import session_lang
from app.graphs.nodes._trace import node_done, node_enter
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

_INTRO_KO = (
    "안녕! 나 kiko야 \U0001f431\n"
    "너 취향에 딱 맞는 옷을 같이 찾아주는 패션 친구지.\n"
    "\n"
    "이렇게 보내주면 내가 찾아줄게:\n"
    "• 핀터레스트 링크 (pin.it / pinterest.com)\n"
    "• 이미지가 보이는 일반 웹 링크\n"
    "• 원하는 스타일·아이템을 글로 설명 — 예: \"미니멀한 블랙 코트 찾아줘\"\n"
    "\n"
    "작은 팁! \U0001f4cc 사진은 파일로 직접 보내는 것보다 링크/URL로 보내주면 제일 잘 찾아. "
    "인스타그램 링크는 아직 못 읽어 — 핀터레스트나 이미지 링크가 좀아!\n"
    "\n"
    "이렇게 편하게 보내바:\n"
    "• \"꺔끔한 화이트 셔츠 추천해줘\"\n"
    "• \"가을에 입을 베이지 트렌치코트\"\n"
    "\n"
    "자, 이제 찾고 싶은 거 보내바. 첫 요청 기다릴게! ✨"
)
_INTRO_EN = (
    "Hey there! I'm kiko \U0001f431\n"
    "Your personal fashion sidekick — I help you find pieces that match your taste.\n"
    "\n"
    "Here's what you can send me:\n"
    "• Pinterest links (pin.it / pinterest.com)\n"
    "• A regular web link with a visible image\n"
    "• Or just describe the style/item in words — e.g. “find me a minimal black coat”\n"
    "\n"
    "Quick tip! \U0001f4cc Photos work best as a link/URL rather than a direct attachment, "
    "and Instagram links aren't supported yet — Pinterest or image links are perfect!\n"
    "\n"
    "Try something like:\n"
    "• “recommend a clean white shirt”\n"
    "• “a beige trench coat for fall”\n"
    "\n"
    "Go ahead and send me what you're looking for — I'm ready for your first request! ✨"
)

_GENDER_CARD_KO = (
    "마지막으로, 누구 옷 찾아줄까? "
    "한 번만 알려주면 다음부터 딸 맞게 골라줄게 \U0001f431"
)
_GENDER_CARD_EN = (
    "One last thing — who are we shopping for? "
    "Tell me once and I'll keep it in mind \U0001f431"
)

_GENDER_BUTTONS_KO = [
    [("\U0001f454 남성", "clarify:gender:men")],
    [("\U0001f457 여성", "clarify:gender:women")],
    [("\U0001f646 상관없음", "clarify:gender:unisex")],
]
_GENDER_BUTTONS_EN = [
    [("\U0001f454 Men", "clarify:gender:men")],
    [("\U0001f457 Women", "clarify:gender:women")],
    [("\U0001f646 Either", "clarify:gender:unisex")],
]


async def _send_gender_card(chat_id: int, lang: str, adapter) -> None:
    """Send the onboarding gender selection card. Best-effort -- never raises.

    Callback shape `clarify:gender:{men|women|unisex}` is consumed inline by
    `ingest._handle_gender_pick` (SPEC-GENDER-PIN-001). When no pending search
    exists (onboarding case), the handler pins the gender and prompts the user
    to start their first request.
    """
    prompt = _GENDER_CARD_KO if lang == "ko" else _GENDER_CARD_EN
    buttons = _GENDER_BUTTONS_KO if lang == "ko" else _GENDER_BUTTONS_EN
    if hasattr(adapter, "send_text_with_keyboard"):
        await adapter.send_text_with_keyboard(chat_id, prompt, buttons)
    elif hasattr(adapter, "send_text_with_buttons"):
        await adapter.send_text_with_buttons(chat_id, prompt, [b[0] for b in buttons])
    else:
        await adapter.send_text(chat_id, prompt)


# @MX:ANCHOR: [AUTO] First-touch intro entry (onboarding-cards OFF path).
# @MX:SPEC: SPEC-AGENT-V2-REACT
# @MX:REASON: routing._route_after_ingest_v2 gates the entire new-user path
#   here; the onboarded_at persist contract (survives next webhook) is what
#   prevents an infinite intro loop.
@observe(name="node.intro", as_type="span")
async def intro(state: WorkingState) -> dict:
    """Send the one-shot service intro + gender card, mark introduced, terminate the turn."""
    node_enter("intro")
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    text = _INTRO_KO if lang == "ko" else _INTRO_EN

    adapter = get_adapter()
    try:
        await adapter.send_text(state.chat_id, text)
    except Exception:  # noqa: BLE001
        logger.exception("\U0001f431 [intro] service intro send failed")

    try:
        await _send_gender_card(state.chat_id, lang, adapter)
    except Exception:  # noqa: BLE001
        logger.debug("\U0001f431 [intro] gender card send best-effort", exc_info=True)

    # Mark introduced + persist (mirrors _onboard_helpers.complete_onboarding
    # step 4). Persisting via the store is what survives the next webhook
    # (fresh graph run) so the user never re-enters intro.
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
            payload={"flow": "intro", "chunk_index": 0, "total_chunks": 1},
        )
    except Exception:  # noqa: BLE001
        logger.debug("\U0001f431 [intro] bot_text emit best-effort", exc_info=True)

    node_done("intro", sent="service_intro", lang=lang)
    return {"log_events": [f"intro: service_intro_sent lang={lang}"]}


__all__ = ["intro"]
