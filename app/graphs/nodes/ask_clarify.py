"""SPEC-AGENT-001 / REQ-AGENT-004 (node 5/10) — ask_clarify.

Generates a single clarifying question via `langchain-openai` against LiteLLM,
sends it via the channel adapter, and ends. Fires only on REQ-AGENT-009's
weak-vision predicate (computed in `routing._is_weak_vision`).

Module-level ChatOpenAI singleton (plan.md Q2): temperature=0.4, max_tokens=80.

REQ-LLM-005: exactly one LLM call. Hard-coded fallback on failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)


_FALLBACK = "Got it — is that a top, a dress, or outerwear?"

_SYSTEM_PROMPT = (
    "You are a friendly fashion-shopping bot. The user shared a photo but the "
    "vision tagger could not pin down what kind of garment it is. Ask ONE short "
    "English clarifying question (under 80 tokens) that lists 3–4 specific "
    "garment categories the user could pick from (e.g. 'top vs dress vs outer'). "
    "No greeting, no preamble, just the question."
)


_llm: Any = None


def _get_llm() -> Any:
    global _llm
    if _llm is None:
        from langchain_openai import ChatOpenAI

        _llm = ChatOpenAI(
            model=settings.RESPONSE_MODEL,
            base_url=settings.LITELLM_BASE_URL + "/v1",
            api_key=settings.LITELLM_MASTER_KEY or "stub",
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


async def ask_clarify(state: WorkingState) -> dict:
    breadcrumbs: list[str] = []
    text = _FALLBACK

    try:
        llm = _get_llm()
        coro = llm.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=_user_prompt(state)),
            ]
        )
        result = await asyncio.wait_for(coro, timeout=max(0.1, settings.RESPONSE_TIMEOUT_MS / 1000.0))
        content = getattr(result, "content", None)
        if isinstance(content, str) and content.strip():
            text = content.strip()[:600]
        else:
            breadcrumbs.append("ask_clarify: empty LLM content → fallback")
    except Exception as exc:  # REQ-AGENT-007 / REQ-LLM-005
        logger.warning("[ask_clarify] LLM failed (%s) → fallback", type(exc).__name__)
        breadcrumbs.append(f"ask_clarify_llm_error: {type(exc).__name__} → fallback")
        text = _FALLBACK

    try:
        adapter = get_adapter()
        await adapter.send_text(state.chat_id, text)
    except Exception as exc:
        logger.exception("[ask_clarify] send_text failed")
        breadcrumbs.append(f"ask_clarify_send_error: {type(exc).__name__}"[:200])
        return {"response_text": text, "log_events": breadcrumbs}

    breadcrumbs.append(f"ask_clarify: text_len={len(text)}")
    return {"response_text": text, "log_events": breadcrumbs}
