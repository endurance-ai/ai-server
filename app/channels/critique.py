"""Critique parsing — converts tap-button callbacks into a structured
`CritiqueDelta` that the recommendation pipeline can apply.

Entry point:
    parse_callback(callback_data, last_results) — deterministic, no LLM
        Used for inline-keyboard taps:  crit:more:2  /  crit:less:0  /  crit:cheap:3

The pipeline consumes `CritiqueDelta` in scenario._run_search via
`ChannelRecommendationRequest.critique_delta`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

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
