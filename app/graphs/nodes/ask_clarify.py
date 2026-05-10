"""SPEC-CLARIFY-CARDS-001 — ask_clarify (인라인 키보드 카드 + legacy 폴백).

이 노드는 weak-vision 분기에서 단 한 번 호출된다. 두 경로:

1. CARD 경로 (REQ-CLARIFY-CARD-001 / settings.CLARIFY_CARDS_ENABLED=true):
   `pick_clarify_axis(vision_result)` 가 non-None 축을 반환하면 결정론적
   인라인 키보드 1개를 보낸다. LLM 호출 없음. session.state =
   AWAITING_CLARIFY 로 전이 후 END.

2. LEGACY 경로 (REQ-CLARIFY-COMPAT-001/002):
   `pick_clarify_axis` 가 None 이거나 `CLARIFY_CARDS_ENABLED=false` 일 때
   기존 자유 텍스트 LLM 한 라운드(SPEC-AGENT-001 시점 동작)로 회귀.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.channels.clarify import ClarifyAxis, pick_clarify_axis
from app.channels.clarify_values import (
    AXIS_PROMPTS_KO,
    SKIP_LABEL_KO,
    SKIP_VALUE,
    get_options,
)
from app.channels.lang import session_lang
from app.channels.session import SessionState, get_store
from app.core.config import settings
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)


_FALLBACK_EN = "Got it — is that a top, a dress, or outerwear?"
_FALLBACK_KO = "넵! 혹시 상의예요, 원피스예요, 아우터예요?"
# Back-compat alias (some older imports may still reference _FALLBACK).
_FALLBACK = _FALLBACK_EN

_SYSTEM_PROMPT_EN = (
    "You are kiko, a playful fashion-shopping bot. The user shared a photo but the "
    "vision tagger could not pin down what kind of garment it is. Ask ONE short "
    "English clarifying question (under 80 tokens) that lists 3–4 specific "
    "garment categories the user could pick from (e.g. 'top vs dress vs outer'). "
    "Bright, bouncy, friendly tone. No greeting, no preamble, just the question."
)

_SYSTEM_PROMPT_KO = (
    "당신은 kiko — 통통 튀고 친근한 패션 쇼핑 봇입니다. 사용자가 사진을 보냈지만 "
    "비전 분석이 어떤 카테고리의 의류인지 확정하지 못했어요. 짧고 밝은 어조의 "
    "해요체 한국어로(반말 NO, 딱딱한 합니다체 NO) 한 문장 질문만 해주세요(최대 80 토큰). "
    "3~4개의 구체적인 의류 카테고리를 보기로 제시하세요(예: 상의 / 원피스 / 아우터 / 하의). "
    "인사말이나 머리말 없이, 질문만."
)

# Back-compat alias
_SYSTEM_PROMPT = _SYSTEM_PROMPT_EN


_llm: Any = None


def _get_llm() -> Any:
    global _llm
    if _llm is None:
        from langchain_openai import ChatOpenAI

        api_key = settings.LITELLM_MASTER_KEY
        if not api_key:
            logger.warning("ask_clarify: LITELLM_MASTER_KEY is empty — using sentinel")
            api_key = "missing-litellm-master-key"
        _llm = ChatOpenAI(
            model=settings.RESPONSE_MODEL,
            base_url=settings.LITELLM_BASE_URL + "/v1",
            api_key=api_key,
            temperature=0.4,
            max_tokens=80,
            timeout=max(0.1, settings.RESPONSE_TIMEOUT_MS / 1000.0),
        )
    return _llm


def _user_prompt(state: WorkingState) -> str:
    items = state.detected_items or []
    if not items:
        return "vision returned no items"
    first = items[0]
    return f"vision result: label={first.get('label', '')!r} description={first.get('description', '')!r}"


def _build_buttons(axis: ClarifyAxis) -> list[tuple[str, str]]:
    """카드 버튼 row 빌드. 마지막에 '건너뛰기' 추가(REQ-CLARIFY-CARD-002).

    상한: settings.CLARIFY_MAX_BUTTONS (기본 5, skip 포함).
    """
    options = get_options(axis.value)
    max_buttons = max(3, min(int(settings.CLARIFY_MAX_BUTTONS), 8))
    # skip 자리(1개) 확보.
    keep = max_buttons - 1
    rows: list[tuple[str, str]] = [(opt.label_ko, f"clarify:{axis.value}:{opt.value}") for opt in options[:keep]]
    rows.append((SKIP_LABEL_KO, f"clarify:{axis.value}:{SKIP_VALUE}"))
    return rows


async def _send_card_path(state: WorkingState, axis: ClarifyAxis) -> dict:
    """CARD 경로 — 인라인 키보드 1개 발행."""
    breadcrumbs: list[str] = []
    body = AXIS_PROMPTS_KO.get(axis.value) or "조금 더 자세히 알려주실 수 있어요?"
    buttons = _build_buttons(axis)

    try:
        adapter = get_adapter()
        if hasattr(adapter, "send_text_with_buttons"):
            await adapter.send_text_with_buttons(state.chat_id, body, buttons)
        else:
            # 어댑터가 인라인 키보드 미지원이면 본문만 송출(graceful).
            await adapter.send_text(state.chat_id, body)
    except Exception as exc:
        logger.exception("[ask_clarify] card send failed")
        breadcrumbs.append(f"ask_clarify_send_error: {type(exc).__name__}"[:200])
        return {"response_text": body, "log_events": breadcrumbs}

    # 세션 상태 전이(REQ-CLARIFY-STATE-001).
    try:
        sess = get_store().get_or_create(state.chat_id)
        sess.state = SessionState.AWAITING_CLARIFY
        sess.clarify_axis = axis.value
        get_store().update(sess)
    except Exception:  # noqa: BLE001
        logger.debug("[ask_clarify] session update best-effort")

    logger.info(
        "[CLARIFY] axis=%s buttons=%d ko_prompt=%r",
        axis.value,
        len(buttons),
        body[:80],
    )
    breadcrumbs.append(f"ask_clarify: card axis={axis.value} buttons={len(buttons)}")
    return {
        "response_text": body,
        "clarify_axis": axis,
        "log_events": breadcrumbs,
    }


async def _legacy_text_path(state: WorkingState) -> dict:
    """REQ-CLARIFY-COMPAT-001/002 — pre-SPEC 자유 텍스트 LLM 한 라운드."""
    breadcrumbs: list[str] = []
    sess = get_store().get_or_create(state.chat_id)
    lang = session_lang(sess)
    sys_prompt = _SYSTEM_PROMPT_KO if lang == "ko" else _SYSTEM_PROMPT_EN
    fallback = _FALLBACK_KO if lang == "ko" else _FALLBACK_EN
    text = fallback

    try:
        llm = _get_llm()
        coro = llm.ainvoke(
            [
                SystemMessage(content=sys_prompt),
                HumanMessage(content=_user_prompt(state)),
            ]
        )
        result = await asyncio.wait_for(coro, timeout=max(0.1, settings.RESPONSE_TIMEOUT_MS / 1000.0))
        content = getattr(result, "content", None)
        if isinstance(content, str) and content.strip():
            text = content.strip()[:600]
        else:
            breadcrumbs.append("ask_clarify: empty LLM content → fallback")
    except Exception as exc:
        logger.warning("🤔 [ask_clarify] ⚠️  LLM failed (%s) → fallback (lang=%s)", type(exc).__name__, lang)
        breadcrumbs.append(f"ask_clarify_llm_error: {type(exc).__name__} → fallback")
        text = fallback

    try:
        adapter = get_adapter()
        await adapter.send_text(state.chat_id, text)
    except Exception as exc:
        logger.exception("[ask_clarify] send_text failed")
        breadcrumbs.append(f"ask_clarify_send_error: {type(exc).__name__}"[:200])
        return {"response_text": text, "log_events": breadcrumbs}

    breadcrumbs.append(f"ask_clarify: legacy_text len={len(text)}")
    return {"response_text": text, "log_events": breadcrumbs}


async def ask_clarify(state: WorkingState) -> dict:
    """REQ-CLARIFY-CARD-001 / REQ-CLARIFY-COMPAT-001/002 — 분기 진입점."""
    if not settings.CLARIFY_CARDS_ENABLED:
        return await _legacy_text_path(state)

    axis = pick_clarify_axis(state.vision_result)
    if axis is None:
        # 결정 불가 — 기존 동작으로 회귀(REQ-CLARIFY-COMPAT-001).
        return await _legacy_text_path(state)

    return await _send_card_path(state, axis)
