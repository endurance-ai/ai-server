"""SPEC-AGENT-001 / REQ-AGENT-004 (node 10/10) — respond.

Generates the final natural-language reply via `langchain-openai` against the
existing LiteLLM proxy (REQ-LLM-002), then dispatches it through the channel
adapter. ALL non-picker / non-clarify flows pass through this node before END
(REQ-AGENT-006).

Module-level singleton (plan.md Q2): one `ChatOpenAI` instance, temperature
0.7, max_tokens=settings.RESPONSE_MAX_TOKENS, timeout=RESPONSE_TIMEOUT_MS.

REQ-LLM-004: exactly one synchronous LLM call per node invocation. On any
failure (timeout / 4xx / 5xx / parse error), respond falls back to a
flow-specific hard-coded English template — REQ-AGENT-007 still applies, the
node never raises.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.channels.router import RoutedIntent
from app.channels.session import get_store
from app.core.config import settings
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)


# ── Hard-coded English fallbacks (REQ-LLM-004) ─────────────────────────────


class _Flow(StrEnum):
    SEARCH_HIT = "search_hit"
    SEARCH_EMPTY = "search_empty"
    LINK_FAIL = "link_fail"
    VISION_FAIL = "vision_fail"
    PHOTO_DIRECT = "photo_direct"
    OFF_TOPIC = "off_topic"
    NEW_SEARCH_NEED_IMAGE = "new_search_need_image"
    TASTE_ACK = "taste_ack"
    REFINE_NUDGE = "refine_nudge"
    PICK_OPENER = "pick_opener"
    DEFAULT = "default"


_FALLBACKS: dict[_Flow, str] = {
    _Flow.SEARCH_HIT: "Tap any to see more like it ✨",
    _Flow.SEARCH_EMPTY: "Hmm, I couldn't find a match — try another angle or a different photo.",
    _Flow.LINK_FAIL: "Sorry, couldn't load that. Try sharing the photo directly.",
    _Flow.VISION_FAIL: "Hmm, I couldn't find a match — try another angle or a different photo.",
    _Flow.PHOTO_DIRECT: (
        "I can't search direct photo uploads yet 🙏\n"
        "Try sharing a Pinterest / image link instead 📌\n"
        "(Direct upload support coming soon!)"
    ),
    _Flow.OFF_TOPIC: "Send me a photo or a Pinterest link first 📸",
    _Flow.NEW_SEARCH_NEED_IMAGE: "Sounds good — share a photo or a Pinterest link to start 📸",
    _Flow.TASTE_ACK: "Noted 📝",
    _Flow.REFINE_NUDGE: "Send me a photo or a Pinterest link first 📸",
    _Flow.PICK_OPENER: "Got it 👌\nSame vibe, something cheaper, or a specific color?",
    _Flow.DEFAULT: "Got it.",
}


def _classify_flow(state: WorkingState) -> _Flow:
    msg = state.message
    sess = get_store().get_or_create(state.chat_id)

    # Bare picker tap (REQ-COMPAT-002): user picked an item but hasn't
    # provided intent yet → ask for vibe/price/color (mirrors original
    # scenario.OPENER_TMPL). Search runs on the next text turn.
    if state.selected_item_index is not None and state.critique_delta is None:
        return _Flow.PICK_OPENER

    # Direct photo upload (no urls)
    if msg.photo_file_id and not msg.urls:
        return _Flow.PHOTO_DIRECT

    # Link-resolution attempted but failed.
    if msg.urls and not state.image_url and not state.detected_items:
        return _Flow.LINK_FAIL

    # Vision attempted but returned empty / fallback.
    if state.image_url and not state.detected_items:
        return _Flow.VISION_FAIL

    # Taste-update path
    if state.decision is not None and state.decision.intent == RoutedIntent.TASTE_UPDATE:
        return _Flow.TASTE_ACK

    # Router branches
    if state.decision is not None:
        if state.decision.intent == RoutedIntent.NEW_SEARCH_REQUEST:
            return _Flow.NEW_SEARCH_NEED_IMAGE
        if state.decision.intent == RoutedIntent.OFF_TOPIC:
            return _Flow.OFF_TOPIC
        if state.decision.intent == RoutedIntent.CRITIQUE_TEXT and (not sess.image_url or not sess.vision_item):
            return _Flow.REFINE_NUDGE

    # Search outcome
    if state.sent_candidates:
        return _Flow.SEARCH_HIT
    if state.candidates is not None and not state.candidates and state.critique_delta is not None:
        return _Flow.SEARCH_EMPTY

    return _Flow.DEFAULT


# ── ChatOpenAI singleton (plan.md Q2) ──────────────────────────────────────


_llm: Any = None


def _get_llm() -> Any:
    global _llm
    if _llm is None:
        from langchain_openai import ChatOpenAI

        api_key = settings.LITELLM_MASTER_KEY
        if not api_key:
            # Tests inject ChatOpenAI mocks before this path runs; if we reach
            # here in real runs without a key, surface it explicitly rather
            # than sending a "stub" string that could match a misconfigured
            # LiteLLM allow-any policy.
            logger.warning("respond: LITELLM_MASTER_KEY is empty — using sentinel")
            api_key = "missing-litellm-master-key"
        _llm = ChatOpenAI(
            model=settings.RESPONSE_MODEL,
            base_url=settings.LITELLM_BASE_URL + "/v1",
            api_key=api_key,
            temperature=0.7,
            max_tokens=settings.RESPONSE_MAX_TOKENS,
            timeout=max(0.1, settings.RESPONSE_TIMEOUT_MS / 1000.0),
        )
    return _llm


_SYSTEM_PROMPT = (
    "You are a friendly fashion-shopping bot replying to a user on Telegram. "
    "Reply in English with a single short conversational message (max ~2 sentences, "
    "under 200 tokens). No markdown headings, no code fences, no JSON, no lists "
    "with newlines longer than two lines. Acknowledge what just happened in the "
    "conversation and (when appropriate) invite the user to take the next step."
)


def _user_prompt(state: WorkingState, flow: _Flow) -> str:
    bits: list[str] = []
    bits.append(f"flow: {flow.value}")
    if state.presearch_summary:
        bits.append(f"presearch_summary: {state.presearch_summary}")
    # For PICK_OPENER use the actual picked item (resolved by index in
    # detected_items) — matches original scenario's OPENER_TMPL.
    if flow == _Flow.PICK_OPENER and state.selected_item_index is not None and state.detected_items:
        idx = state.selected_item_index
        if 0 <= idx < len(state.detected_items):
            picked = state.detected_items[idx]
            bits.append(f"picked_item: {picked.get('label', '')}")
    elif state.detected_items:
        first = state.detected_items[0]
        bits.append(f"detected_item: {first.get('label', '')}")
    if state.sent_candidates:
        bits.append(f"sent_count: {len(state.sent_candidates)}")
    if not bits:
        bits.append("no extra context")
    return "; ".join(bits)


# ── Node ───────────────────────────────────────────────────────────────────


async def respond(state: WorkingState) -> dict:
    flow = _classify_flow(state)
    fallback_text = _FALLBACKS[flow]

    # Build prompt
    sys_msgs = [SystemMessage(content=_SYSTEM_PROMPT)]
    sys_msgs.extend(state.messages or [])
    sys_msgs.append(HumanMessage(content=_user_prompt(state, flow)))

    text = fallback_text
    breadcrumbs: list[str] = []
    try:
        llm = _get_llm()
        coro = llm.ainvoke(sys_msgs)
        result = await asyncio.wait_for(coro, timeout=max(0.1, settings.RESPONSE_TIMEOUT_MS / 1000.0))
        content = getattr(result, "content", None)
        if isinstance(content, str) and content.strip():
            # Strip leading/trailing whitespace, cap to safety length.
            text = content.strip()[: 4 * settings.RESPONSE_MAX_TOKENS]
        else:
            breadcrumbs.append("respond: empty LLM content → fallback")
    except Exception as exc:  # REQ-AGENT-007 / REQ-LLM-004
        logger.warning("[respond] LLM failed (%s) → fallback", type(exc).__name__)
        breadcrumbs.append(f"respond_llm_error: {type(exc).__name__} → fallback")
        text = fallback_text

    # Dispatch
    try:
        adapter = get_adapter()
        await adapter.send_text(state.chat_id, text)
    except Exception as exc:
        logger.exception("[respond] send_text failed")
        breadcrumbs.append(f"respond_send_error: {type(exc).__name__}: {exc}"[:200])
        return {"response_text": text, "log_events": breadcrumbs}

    breadcrumbs.append(f"respond: flow={flow.value} text_len={len(text)}")
    return {"response_text": text, "log_events": breadcrumbs}
