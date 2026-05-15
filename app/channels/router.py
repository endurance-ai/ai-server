"""DEPRECATED (when AGENT_V2_REACT_ENABLED=true) — superseded by SPEC-AGENT-V2-REACT.
The agent LLM's reasoning subsumes deterministic text routing. Retained for V2.0
rollback safety (V1 path still uses this). Will be removed in V2.1 cleanup
(see SPEC-AGENT-V2-CLEANUP-001).

@MX:LEGACY: superseded by SPEC-AGENT-V2-REACT — V2.1 removal target

Routing-LLM — paraphrase-aware text classification.

Sits between `scenario.classify_input` (deterministic prefilter) and the
handler dispatch. Only invoked when the prefilter cannot type a text message
on its own — most callback/photo/digit-pick paths bypass this entirely.

Single LLM call covers the two states where the SM is structurally ambiguous:
    - RESULTS_SENT  + text  →  refine vs taste_update vs new_search
    - IDLE          + text  →  greeting vs taste_update vs request_for_image

Output is a `RoutedDecision` whose `trigger` slots into the existing Trigger
enum + two new ones (CRITIQUE_TEXT, TASTE_UPDATE), and whose payload carries
the parsed CritiqueDelta or TasteUpdate so handlers don't need to re-call the
LLM.

Failure mode: any LLM problem returns a deterministic best-guess based on
state — RESULTS_SENT → CRITIQUE_TEXT(extra_intent=text), IDLE → TEXT_FALLBACK.
The bot stays usable with the LLM offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

from app.channels.critique import CritiqueDelta
from app.channels.session import SessionState
from app.core.config import settings
from app.providers.llm import LLMProvider

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class RoutedIntent(StrEnum):
    """Subset of trigger semantics produced by the router. Maps 1:1 to
    scenario.Trigger values; kept separate so router stays decoupled from
    scenario module imports."""

    NEW_SEARCH_REQUEST = "new_search_request"  # "find me ..." with no current image — bot asks for image
    CRITIQUE_TEXT = "critique_text"  # refine current results
    TASTE_UPDATE = "taste_update"  # explicit preference statement
    OFF_TOPIC = "off_topic"  # greetings, thanks, irrelevant


@dataclass
class TasteUpdate:
    liked_brands: list[str] = field(default_factory=list)
    disliked_brands: list[str] = field(default_factory=list)
    liked_keywords: list[str] = field(default_factory=list)
    disliked_keywords: list[str] = field(default_factory=list)


@dataclass
class RoutedDecision:
    intent: RoutedIntent
    critique_delta: CritiqueDelta | None = None
    taste_update: TasteUpdate | None = None


_ROUTER_SYSTEM_PROMPT = (
    "You classify a user's chat message in a fashion-shopping bot. The bot has "
    "already shown the user a set of recommended items. Given the user's reply "
    "and (optionally) the items shown, output JSON only.\n"
    'Schema: {"intent": "<one of: critique_text|taste_update|new_search_request|off_topic>", '
    '"critique": {"exclude_brands":[str], "exclude_keywords":[str], '
    '"boost_keywords":[str], "max_price":int|null, "min_price":int|null, '
    '"color":str|null, "extra_intent":str|null} | null, '
    '"taste": {"liked_brands":[str], "disliked_brands":[str], '
    '"liked_keywords":[str], "disliked_keywords":[str]} | null}\n'
    "Rules:\n"
    "- intent=critique_text: user wants to refine the current results "
    '("cheaper", "in black", "less denim", "more like the second one")\n'
    "- intent=taste_update: user states a durable preference unrelated to current "
    'results ("I love ami paris", "I hate fast fashion brands")\n'
    "- intent=new_search_request: user wants a fresh search but provides only text "
    '(no image attached); usually phrased as a question or wish ("show me beige tees")\n'
    "- intent=off_topic: greetings, thanks, complaints unrelated to shopping\n"
    "- Populate critique only when intent=critique_text. Populate taste only when "
    "intent=taste_update. Otherwise null.\n"
    "- max_price/min_price are integers in KRW (₩). null if not specified.\n"
    "- All lists may be empty. Never invent attributes not implied by the input.\n"
    "- Korean and English inputs are both expected; do not translate, just parse."
)


def _build_user_payload(text: str, last_results: list[Any]) -> str:
    items_summary: list[dict[str, Any]] = []
    for i, c in enumerate(last_results[:5]):
        items_summary.append(
            {
                "idx": i,
                "brand": (getattr(c, "brand", "") or "")[:40],
                "name": (getattr(c, "name", "") or "")[:60],
                "price": getattr(c, "price", None),
            }
        )
    payload = {"user_text": (text or "")[:300], "shown_items": items_summary}
    return json.dumps(payload, ensure_ascii=False)


def _str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()][:10]


def _opt_int(v: Any) -> int | None:
    try:
        n = int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return n if n and n > 0 else None


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_router_output(content: str, raw_text: str) -> RoutedDecision:
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(content)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        return _fallback_critique(raw_text)

    intent_raw = str(parsed.get("intent") or "").strip()
    try:
        intent = RoutedIntent(intent_raw)
    except ValueError:
        return _fallback_critique(raw_text)

    if intent == RoutedIntent.CRITIQUE_TEXT:
        c = parsed.get("critique") or {}
        delta = CritiqueDelta(
            op="free_text",
            exclude_brands=_str_list(c.get("exclude_brands")),
            exclude_keywords=_str_list(c.get("exclude_keywords")),
            boost_keywords=_str_list(c.get("boost_keywords")),
            max_price=_opt_int(c.get("max_price")),
            min_price=_opt_int(c.get("min_price")),
            color=_opt_str(c.get("color")),
            extra_intent=_opt_str(c.get("extra_intent")) or raw_text[:200],
        )
        return RoutedDecision(intent=intent, critique_delta=delta)

    if intent == RoutedIntent.TASTE_UPDATE:
        t = parsed.get("taste") or {}
        update = TasteUpdate(
            liked_brands=_str_list(t.get("liked_brands")),
            disliked_brands=_str_list(t.get("disliked_brands")),
            liked_keywords=_str_list(t.get("liked_keywords")),
            disliked_keywords=_str_list(t.get("disliked_keywords")),
        )
        return RoutedDecision(intent=intent, taste_update=update)

    return RoutedDecision(intent=intent)


def _fallback_critique(text: str) -> RoutedDecision:
    """When the LLM is unavailable / output is malformed, treat ambiguous
    text in RESULTS_SENT as a free-text critique with the raw string as the
    extra-intent signal. The pipeline still gets a usable hint."""
    return RoutedDecision(
        intent=RoutedIntent.CRITIQUE_TEXT,
        critique_delta=CritiqueDelta(op="free_text", extra_intent=(text or "")[:200]),
    )


async def route_text(text: str, state: SessionState, last_results: list[Any]) -> RoutedDecision:
    """Classify ambiguous text. Caller must have ensured the deterministic
    prefilter could not type the message (e.g. no callback, no photo, no url,
    state ∈ {RESULTS_SENT, IDLE})."""
    raw = (text or "").strip()
    if not raw:
        return RoutedDecision(intent=RoutedIntent.OFF_TOPIC)

    if not settings.ROUTER_LLM_ENABLED:
        return (
            _fallback_critique(raw)
            if state == SessionState.RESULTS_SENT
            else RoutedDecision(intent=RoutedIntent.OFF_TOPIC)
        )

    timeout_s = max(0.001, settings.ROUTER_TIMEOUT_MS / 1000.0)
    messages = [
        {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_payload(raw, last_results)},
    ]
    try:
        resp = await asyncio.wait_for(
            LLMProvider.chat(
                model=settings.ROUTER_MODEL,
                messages=messages,
                temperature=0.1,
                max_tokens=settings.ROUTER_MAX_TOKENS,
                response_format={"type": "json_object"},
            ),
            timeout=timeout_s,
        )
    except (TimeoutError, httpx.HTTPError) as exc:
        logger.warning(
            "🧭 [router] ⚠️  LLM 호출 실패 (%s: %s) → fallback (state=%s, text=%r, timeout=%.1fs)",
            type(exc).__name__,
            str(exc)[:120] or "(no detail)",
            state.value,
            raw[:80],
            timeout_s,
        )
        return (
            _fallback_critique(raw)
            if state == SessionState.RESULTS_SENT
            else RoutedDecision(intent=RoutedIntent.OFF_TOPIC)
        )
    except Exception:
        logger.exception("🧭 [router] ❌ 알 수 없는 LLM 오류 → fallback (state=%s, text=%r)", state.value, raw[:80])
        return (
            _fallback_critique(raw)
            if state == SessionState.RESULTS_SENT
            else RoutedDecision(intent=RoutedIntent.OFF_TOPIC)
        )

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return (
            _fallback_critique(raw)
            if state == SessionState.RESULTS_SENT
            else RoutedDecision(intent=RoutedIntent.OFF_TOPIC)
        )
    if not isinstance(content, str) or not content.strip():
        return (
            _fallback_critique(raw)
            if state == SessionState.RESULTS_SENT
            else RoutedDecision(intent=RoutedIntent.OFF_TOPIC)
        )

    decision = _parse_router_output(content, raw)
    logger.info(
        "🧭 [router] state=%s → intent=%s delta=%s taste=%s text=%r",
        state.value,
        decision.intent.value,
        bool(decision.critique_delta),
        bool(decision.taste_update),
        raw[:80],
    )
    return decision
