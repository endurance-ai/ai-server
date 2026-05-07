"""SPEC-AGENTIC-CRITIQUE-001 — Pydantic schemas for the self-critique evaluator.

Distinct from `app.channels.critique.CritiqueDelta` (the user-driven dataclass
critique helper from PR #10) — this module owns the **internal self-critique**
schemas. The evaluator emits `CritiqueScore`; the search node consumes
`CritiqueDelta` (this one) on retry. Pydantic v2 with `extra="forbid"` so any
drift between the evaluator's prompt-induced JSON and the consumer fails fast.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CritiqueDelta", "CritiqueScore"]


_INTENT_VALUES = Literal[
    "broaden",
    "narrow",
    "refine_color",
    "refine_fit",
    "exclude_brands",
    "exclude_keywords",
]


class CritiqueDelta(BaseModel):
    """Refinement description emitted by the self-critique evaluator.

    Internal-only — distinct from `app.channels.critique.CritiqueDelta`
    (user-driven). Mirrors the same axes so downstream wiring (search_node)
    can layer either flavor onto the request.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intent: _INTENT_VALUES
    boost_keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    exclude_brands: list[str] = Field(default_factory=list)
    color_override: str | None = None
    fit_override: str | None = None
    drop_min_price: bool = False
    drop_max_price: bool = False
    drop_filters: list[str] = Field(default_factory=list)


class CritiqueScore(BaseModel):
    """Output contract of the evaluator node.

    `score` ∈ [0.0, 1.0] — 1.0 = perfect match; 0.0 = useless results.
    `retry=True` requires a `suggested_delta` (defensive — REQ-CRITIQUE-EVAL-002).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    score: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(default="", max_length=400)
    suggested_delta: CritiqueDelta | None = None
    retry: bool = False


def deltas_equivalent(a: CritiqueDelta | None, b: CritiqueDelta | None) -> bool:
    """REQ-CRITIQUE-LOOP-SAFETY-002 — set-equivalence on the salient fields.

    Two deltas are stagnation-equivalent when their (intent, exclude_brands,
    exclude_keywords, boost_keywords, color_override, fit_override,
    drop_filters) match as sets / scalars. Order-insensitive.
    """
    if a is None or b is None:
        return a is b

    def _norm(values: list[str]) -> frozenset[str]:
        return frozenset(v.strip().lower() for v in values if v and v.strip())

    return (
        a.intent == b.intent
        and _norm(a.exclude_brands) == _norm(b.exclude_brands)
        and _norm(a.exclude_keywords) == _norm(b.exclude_keywords)
        and _norm(a.boost_keywords) == _norm(b.boost_keywords)
        and (a.color_override or "").strip().lower() == (b.color_override or "").strip().lower()
        and (a.fit_override or "").strip().lower() == (b.fit_override or "").strip().lower()
        and _norm(a.drop_filters) == _norm(b.drop_filters)
        and a.drop_min_price == b.drop_min_price
        and a.drop_max_price == b.drop_max_price
    )
