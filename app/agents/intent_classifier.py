"""Modifier-intent classifier for the multi-turn search router.

After a Vision turn populates an origin image, every follow-up text turn
("make it blue", "더 캐주얼하게", "오버사이즈로") needs a different blending
strategy:

    color_swap            -> vector arithmetic (image - old_color + new_color)
    fit_change            -> vector arithmetic (image - old_fit   + new_fit)
    mood_shift            -> weighted-sum, alpha=0.3 (modifier-heavy)
    identity_preservation -> weighted-sum, alpha=0.5 (balanced)
    free_form             -> weighted-sum, alpha=0.7 (default, safest)

This module owns the LLM call that maps a user follow-up message to one of
the five intent kinds plus -- for color_swap / fit_change -- the FROM and TO
attribute tokens that drive the arithmetic.

Single-turn budget impact:
  ~1 LiteLLM call per refine turn, ~120 input tokens / ~80 output tokens.
  Marginal cost; cached implicitly because each turn has a unique input.

Lifecycle:
  Pure function. No global state, no caching. The caller (refine router)
  decides whether to invoke. Errors fall through to `free_form` so the search
  pipeline never blocks on classifier failure.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# --- Result schema ---------------------------------------------------------


@dataclass(frozen=True)
class IntentResult:
    intent: str  # one of: color_swap, fit_change, mood_shift, identity_preservation, free_form
    from_attribute: str | None = None  # populated for color_swap, fit_change
    to_attribute: str | None = None  # populated for color_swap, fit_change
    confidence: float = 0.0
    raw: dict[str, Any] | None = None


_VALID_INTENTS = (
    "color_swap",
    "fit_change",
    "mood_shift",
    "identity_preservation",
    "free_form",
)


def _fallback() -> IntentResult:
    return IntentResult(intent="free_form", confidence=0.0)


# --- Prompt ----------------------------------------------------------------


_SYSTEM_PROMPT = """You classify a user's fashion follow-up message into ONE
of five intent kinds for a multi-turn search router. Reply with a JSON object
only -- no prose, no code fences.

Schema:
{
  "intent": "color_swap" | "fit_change" | "mood_shift" | "identity_preservation" | "free_form",
  "from_attribute": "...",   // ONLY for color_swap or fit_change. The attribute
                             // being REPLACED. Use the most specific English
                             // token from the prior outfit (e.g. "grey",
                             // "slim"). Use "" if unknown.
  "to_attribute": "...",     // ONLY for color_swap or fit_change. The NEW
                             // attribute the user wants. Use the most
                             // specific English token from the user's
                             // message (e.g. "blue navy", "oversized").
  "confidence": 0.0-1.0      // your own confidence
}

Intent definitions:
- color_swap   : user wants the SAME garment in a different colour.
                 triggers: "make it blue", "다른 색으로", "파란색으로",
                 "검정으로 보여줘", "in red", "olive 버전".
                 from_attribute  = the prior outfit's colour token (English)
                 to_attribute    = the new colour token (English, lowercase)
- fit_change   : user wants the SAME garment in a different fit/silhouette.
                 triggers: "오버사이즈로", "slim fit", "wide leg", "더 박시하게".
                 from_attribute  = the prior fit token (English)
                 to_attribute    = the new fit token (English, lowercase)
- mood_shift   : user wants the SAME category but a different mood/vibe.
                 triggers: "더 캐주얼하게", "more vintage", "minimal 느낌",
                 "streetwear style". from/to_attribute = "".
- identity_preservation : user wants more of the SAME outfit; mild
                 refinement. triggers: "비슷한 거 더", "show more like this",
                 "더 보여줘", "again". from/to_attribute = "".
- free_form    : anything else, including ambiguous or compound asks. Use
                 when the message changes the SEARCH TARGET (e.g. "now
                 trousers instead", "show me shoes"). from/to_attribute = "".

Rules:
- Prior outfit attributes are passed in as `prior_outfit:` context. If
  absent, leave from_attribute empty.
- Korean colour/fit tokens MUST be translated to canonical English:
  "검정"->"black", "파란색/네이비"->"navy blue", "오버사이즈"->"oversized",
  "와이드"->"wide", "박시"->"boxy", "슬림"->"slim".
- When unsure between color_swap and mood_shift, prefer mood_shift if the
  message does NOT contain a concrete colour word."""


def _user_prompt(message: str, prior_outfit_context: str | None) -> str:
    ctx = prior_outfit_context.strip() if prior_outfit_context else "(none)"
    return f"prior_outfit: {ctx}\nfollow_up_message: {message!r}\n\nClassify per the schema."


# --- Parsing ---------------------------------------------------------------


def _parse_json_relaxed(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        # strip optional ``` / ```json fence
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _normalise_result(parsed: dict) -> IntentResult:
    intent = str(parsed.get("intent") or "").strip().lower()
    if intent not in _VALID_INTENTS:
        return _fallback()

    def _attr(key: str) -> str | None:
        v = parsed.get(key)
        if not v:
            return None
        s = str(v).strip().lower()
        return s or None

    from_attr = _attr("from_attribute") if intent in ("color_swap", "fit_change") else None
    to_attr = _attr("to_attribute") if intent in ("color_swap", "fit_change") else None

    try:
        confidence = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    return IntentResult(
        intent=intent,
        from_attribute=from_attr,
        to_attribute=to_attr,
        confidence=max(0.0, min(1.0, confidence)),
        raw=parsed,
    )


# --- Public API ------------------------------------------------------------


async def classify_intent(
    message: str,
    prior_outfit_context: str | None = None,
    *,
    litellm_call: Any | None = None,
    model: str = "claude-haiku-4-5",
    timeout_s: float = 8.0,
) -> IntentResult:
    """Classify a refine message.

    Parameters:
        message               -- the user's follow-up text (raw).
        prior_outfit_context  -- a short English summary of the prior outfit
                                 to anchor `from_attribute` extraction. E.g.
                                 "grey wool oversized overcoat men".
        litellm_call          -- async callable `(model, messages) -> str` for
                                 dependency injection in tests. When None,
                                 we resolve `app.providers.llm.LLMProvider`.
        model                 -- LiteLLM model name (default Claude Haiku).
        timeout_s             -- per-call wall-clock timeout.

    Returns IntentResult; falls back to `free_form` on any failure.
    """
    if not message or not message.strip():
        return _fallback()

    if litellm_call is None:
        litellm_call = _default_litellm_call

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(message, prior_outfit_context)},
    ]

    import asyncio

    try:
        content = await asyncio.wait_for(
            litellm_call(model=model, messages=messages),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[intent_classifier] LLM call failed: %r", exc)
        return _fallback()

    parsed = _parse_json_relaxed(content or "")
    if parsed is None:
        logger.debug("[intent_classifier] JSON parse failed: %r", content[:200] if content else "")
        return _fallback()

    result = _normalise_result(parsed)
    logger.info(
        "🎯 [intent] %s confidence=%.2f from=%r to=%r message=%r",
        result.intent,
        result.confidence,
        result.from_attribute,
        result.to_attribute,
        message[:60],
    )
    return result


async def _default_litellm_call(*, model: str, messages: list[dict[str, Any]]) -> str:
    """Production LiteLLM call. Kept thin so tests can swap it out."""
    from app.providers.llm import LLMProvider

    client = LLMProvider.get_client()
    resp = await client.post(
        "/chat/completions",
        json={
            "model": model,
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 200,
        },
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload["choices"][0]["message"]["content"]


__all__ = ["IntentResult", "classify_intent"]
