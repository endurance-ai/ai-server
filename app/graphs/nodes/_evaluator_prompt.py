"""SPEC-AGENTIC-CRITIQUE-001 / REQ-CRITIQUE-EVAL-003 — evaluator prompt strings.

Mirrors the `vision_prompt.py` pattern: prompt body kept in a sibling module
so iteration on the prompt does not produce diffs in the node logic and so
prompt text can be unit-tested independently.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["SYSTEM_PROMPT", "build_user_prompt"]


SYSTEM_PROMPT = (
    "You are a fashion shopping search-result evaluator. Given (a) the user's "
    "intended item (subcategory, colorFamily, fit, search query) and (b) the "
    "top product candidates returned by the search, judge whether the candidates "
    "actually match the user's intent.\n\n"
    "Output ONLY a single JSON object matching this schema, no prose, no markdown:\n"
    "{\n"
    '  "score": float in [0.0, 1.0],   // 1.0 = perfect match, 0.0 = useless\n'
    '  "reasoning": string,            // <= 200 chars, English\n'
    '  "retry": boolean,               // true ONLY if you can suggest a refinement\n'
    '  "suggested_delta": null OR {\n'
    '     "intent": one of ["broaden","narrow","refine_color","refine_fit",'
    '"exclude_brands","exclude_keywords"],\n'
    '     "boost_keywords": [string], "exclude_keywords": [string],\n'
    '     "exclude_brands": [string], "color_override": string|null,\n'
    '     "fit_override": string|null, "drop_min_price": bool,\n'
    '     "drop_max_price": bool, "drop_filters": [string]\n'
    "  }\n"
    "}\n\n"
    "Rules:\n"
    "- Score >= 0.6 means the user will likely be satisfied.\n"
    "- If score >= 0.6, set retry=false and suggested_delta=null.\n"
    "- If retry=true, suggested_delta MUST be populated and MUST strictly improve match quality.\n"
    "- Prefer broaden when candidates are 0 or far too few.\n"
    "- Prefer narrow / refine_color / refine_fit when candidates are off-target.\n"
    "- Do NOT invent brands or keywords not implied by the candidates.\n"
)


def _summarize_candidate(candidate: Any, idx: int) -> dict[str, Any]:
    """Compact text-only projection — keeps prompt tokens minimal."""
    return {
        "i": idx,
        "brand": (getattr(candidate, "brand", "") or "").strip()[:40],
        "name": (getattr(candidate, "name", "") or "").strip()[:80],
        "subcategory": (getattr(candidate, "subcategory", "") or "").strip()[:40],
        "price": getattr(candidate, "price", None),
    }


def build_user_prompt(
    *,
    vision_item: Any | None,
    user_intent: str | None,
    candidates: list[Any],
    top_n: int = 5,
) -> str:
    """Render the user prompt as compact JSON. Trimmed to top_n candidates."""
    item_summary: dict[str, Any] = {}
    if vision_item is not None:
        item_summary = {
            "subcategory": (getattr(vision_item, "subcategory", "") or "")[:40],
            "fit": (getattr(vision_item, "fit", "") or "")[:40],
            "colorFamily": (getattr(vision_item, "colorFamily", "") or "")[:40],
            "searchQuery": (getattr(vision_item, "searchQuery", "") or "")[:120],
            "searchQueryKo": (getattr(vision_item, "searchQueryKo", "") or "")[:120],
        }

    payload = {
        "intended_item": item_summary,
        "user_intent": (user_intent or "")[:200],
        "candidates_count": len(candidates),
        "candidates": [_summarize_candidate(c, i) for i, c in enumerate(candidates[:top_n])],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
