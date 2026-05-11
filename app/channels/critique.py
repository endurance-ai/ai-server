"""Critique parsing — converts tap-button callbacks AND free-text critique
into a structured `CritiqueDelta` that the recommendation pipeline can apply.

Two entry points:
    parse_callback(callback_data, last_results) — deterministic, no LLM
        Used for inline-keyboard taps:  crit:more:2  /  crit:less:0  /  crit:cheap:3
    parse_text(text, last_results)               — LLM-backed
        Used for free-form critique replies in RESULTS_SENT.

The pipeline consumes `CritiqueDelta` in scenario._run_search via
`ChannelRecommendationRequest.critique_delta`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.providers.llm import LLMProvider

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

# crit:{op}:{idx}  where op ∈ {more, less, cheap}, idx ∈ 0..N-1
_CALLBACK_RE = re.compile(r"^crit:(more|less|cheap):(\d+)$")


@dataclass(frozen=True)
class AnchorRef:
    """Reference to a card from the previous result set."""

    idx: int
    product_id: str | None
    brand: str | None
    name: str | None
    price: int | None
    keywords: list[str] = field(default_factory=list)


@dataclass
class CritiqueDelta:
    """Structured refinement to layer on top of the previous search.

    All fields are optional — handler applies whichever are populated.
    """

    op: str  # "more" | "less" | "cheap" | "free_text" | "noop"
    anchor: AnchorRef | None = None
    exclude_brands: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    boost_keywords: list[str] = field(default_factory=list)
    max_price: int | None = None
    min_price: int | None = None
    color: str | None = None
    extra_intent: str | None = None  # additional natural-language hint to layer on intent


from app.channels._candidate_attr import attr as _attr  # noqa: E402


def _candidate_to_anchor(c: Any, idx: int) -> AnchorRef:
    pid = _attr(c, "id")
    return AnchorRef(
        idx=idx,
        product_id=str(pid) if pid is not None else None,
        brand=(_attr(c, "brand", "") or "").strip() or None,
        name=(_attr(c, "name", "") or "").strip() or None,
        price=_attr(c, "price"),
        keywords=[],  # populated only if we cache vision keywords per card later
    )


def parse_callback(callback_data: str, last_results: list[Any]) -> CritiqueDelta | None:
    """Map an inline-keyboard tap into a CritiqueDelta. Returns None on
    invalid format or out-of-range index (caller should answer the callback
    with an error toast and otherwise ignore)."""
    m = _CALLBACK_RE.match(callback_data or "")
    if not m:
        return None
    op = m.group(1)
    try:
        idx = int(m.group(2))
    except ValueError:
        return None
    if not (0 <= idx < len(last_results)):
        return None
    anchor = _candidate_to_anchor(last_results[idx], idx)

    if op == "more":
        # boost on this product's brand + keywords; signal flows through search
        # via boost_keywords (sparse) and via the embedding layer post-RPC
        # (Python re-rank). For now: just record the anchor; runner extracts.
        return CritiqueDelta(op="more", anchor=anchor, boost_keywords=anchor.keywords)
    if op == "less":
        excl_brands = [anchor.brand] if anchor.brand else []
        return CritiqueDelta(op="less", anchor=anchor, exclude_brands=excl_brands)
    if op == "cheap":
        max_price = None
        if anchor.price and anchor.price > 0:
            max_price = int(anchor.price * settings.CRITIQUE_CHEAPER_RATIO)
        return CritiqueDelta(op="cheap", anchor=anchor, max_price=max_price)
    return None


_CRITIQUE_SYSTEM_PROMPT = (
    "You parse a fashion shopping user's free-text critique of search results "
    "into a structured JSON delta. Output JSON only, no prose.\n"
    'Schema: {"exclude_brands":[str], "exclude_keywords":[str], '
    '"boost_keywords":[str], "max_price":int|null, "min_price":int|null, '
    '"color":str|null, "extra_intent":str|null}\n'
    "Rules:\n"
    "- exclude_keywords: garment/style words user wants to remove (e.g. 'denim', 'cropped')\n"
    "- boost_keywords: garment/style words user wants more of\n"
    "- max_price/min_price: integer in KRW (₩); null if unspecified\n"
    "- color: dominant color word user is requesting; null if unspecified\n"
    "- extra_intent: any other directive in 1 short English phrase, or null\n"
    "- All lists may be empty. Do not invent attributes not implied by the input."
)


async def parse_text(text: str, last_results: list[Any]) -> CritiqueDelta:
    """LLM-backed free-text critique parser. Always returns a CritiqueDelta
    (never raises) — falls back to op='free_text' with extra_intent=text on
    any failure so the search at least sees the raw signal."""
    raw = (text or "").strip()
    if not raw:
        return CritiqueDelta(op="noop")

    fallback = CritiqueDelta(op="free_text", extra_intent=raw[:200])

    if not settings.ROUTER_LLM_ENABLED:
        return fallback

    timeout_s = max(0.001, settings.ROUTER_TIMEOUT_MS / 1000.0)
    messages = [
        {"role": "system", "content": _CRITIQUE_SYSTEM_PROMPT},
        {"role": "user", "content": raw[:300]},
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
    except (TimeoutError, httpx.HTTPError):
        logger.warning("[critique] LLM 호출 실패 → free_text fallback")
        return fallback
    except Exception:
        logger.exception("[critique] 알 수 없는 LLM 오류 → free_text fallback")
        return fallback

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return fallback
    if not isinstance(content, str) or not content.strip():
        return fallback

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
        return fallback

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

    delta = CritiqueDelta(
        op="free_text",
        exclude_brands=_str_list(parsed.get("exclude_brands")),
        exclude_keywords=_str_list(parsed.get("exclude_keywords")),
        boost_keywords=_str_list(parsed.get("boost_keywords")),
        max_price=_opt_int(parsed.get("max_price")),
        min_price=_opt_int(parsed.get("min_price")),
        color=(str(parsed.get("color")).strip() if parsed.get("color") else None),
        extra_intent=(str(parsed.get("extra_intent")).strip() if parsed.get("extra_intent") else raw[:200]),
    )
    return delta
