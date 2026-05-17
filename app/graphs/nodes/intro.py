"""SPEC-AGENT-V2-REACT — first-touch service intro (onboarding-cards OFF).

When `ONBOARDING_CARDS_ENABLED=false` the mood/color/fit/pinterest card flow
is skipped (routing.onboarding_required → False). A brand-new user
(`sess.onboarded_at IS NULL`) would otherwise fall straight into the V2 agent
with no introduction. This node sends ONE deterministic, detailed, friendly
intro message, marks the user as introduced (`sess.onboarded_at = now()`,
persisted via the session store — mirrors `_onboard_helpers.complete_onboarding`
step 4), and terminates the turn (the first message is NOT processed by the
agent). From the user's 2nd message onward the normal V2 agent flow runs.

@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.channels.factory import get_adapter
from app.channels.lang import session_lang
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

_INTRO_KO = (
    "안녕하세요! 저는 kiko예요 🐱\n"
    "당신 취향에 딱 맞는 옷을 같이 찾아주는 패션 친구랍니다.\n"
    "\n"
    "이렇게 보내주시면 제가 찾아드려요:\n"
    "• 핀터레스트 링크 (pin.it / pinterest.com)\n"
    "• 이미지가 보이는 일반 웹 링크\n"
    "• 원하는 스타일·아이템을 글로 설명 — 예: “미니멀한 블랙 코트 찾아줘”\n"
    "\n"
    "작은 팁! 📌 사진은 파일로 직접 보내는 것보다 링크/URL로 보내주시면 제일 잘 찾아요. "
    "인스타그램 링크는 아직 못 읽어요 — 핀터레스트나 이미지 링크를 추천해요!\n"
    "\n"
    "예시처럼 편하게 보내보세요:\n"
    "• “깔끔한 화이트 셔츠 추천해줘”\n"
    "• “가을에 입을 베이지 트렌치코트”\n"
    "\n"
    "자, 이제 찾고 싶은 걸 보내주세요. 첫 요청 기다리고 있을게요! ✨"
)
_INTRO_EN = (
    "Hey there! I'm kiko 🐱\n"
    "Your personal fashion sidekick — I help you find pieces that match your taste.\n"
    "\n"
    "Here's what you can send me:\n"
    "• Pinterest links (pin.it / pinterest.com)\n"
    "• A regular web link with a visible image\n"
    "• Or just describe the style/item in words — e.g. “find me a minimal black coat”\n"
    "\n"
    "Quick tip! 📌 Photos work best as a link/URL rather than a direct attachment, "
    "and Instagram links aren't supported yet — Pinterest or image links are perfect!\n"
    "\n"
    "Try something like:\n"
    "• “recommend a clean white shirt”\n"
    "• “a beige trench coat for fall”\n"
    "\n"
    "Go ahead and send me what you're looking for — I'm ready for your first request! ✨"
)


# @MX:ANCHOR: [AUTO] First-touch intro entry (onboarding-cards OFF path).
# @MX:SPEC: SPEC-AGENT-V2-REACT
# @MX:REASON: routing._route_after_ingest_v2 gates the entire new-user path
#   here; the onboarded_at persist contract (survives next webhook) is what
#   prevents an infinite intro loop.
@observe(name="node.intro", as_type="span")
async def intro(state: WorkingState) -> dict:
    """Send the one-shot service intro, mark introduced, terminate the turn."""
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    text = _INTRO_KO if lang == "ko" else _INTRO_EN

    try:
        await get_adapter().send_text(state.chat_id, text)
    except Exception:  # noqa: BLE001
        logger.exception("🐱 [intro] service intro send failed")

    # Mark introduced + persist (mirrors _onboard_helpers.complete_onboarding
    # step 4). Persisting via the store is what survives the next webhook
    # (fresh graph run) so the user never re-enters intro.
    try:
        sess.onboarded_at = datetime.now(UTC)
        get_store().update(sess)
    except Exception:  # noqa: BLE001
        logger.exception("🐱 [intro] onboarded_at persist failed")

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
        logger.debug("🐱 [intro] bot_text emit best-effort", exc_info=True)

    return {"log_events": [f"intro: service_intro_sent lang={lang}"]}


__all__ = ["intro"]
