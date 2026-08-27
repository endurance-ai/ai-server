"""Recommendation port — channel-side abstraction over the search pipeline.

Decouples `app.channels.scenario` from `app.pipeline.runner` so the messenger
flow can later be split into its own process / repo without rewriting scenario
logic. Channels speak in `ChannelRecommendationRequest` (intent + keywords +
image), not the REST DTO `RecommendRequest`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.models.response import Candidate

__all__ = [
    "Candidate",
    "ChannelRecommendationRequest",
    "ChannelRecommendationResult",
    "PipelineRecommendationPort",
    "RecommendationPort",
    "get_port",
    "set_port",
]


@dataclass(frozen=True)
class ChannelRecommendationRequest:
    """Channel-friendly recommendation input.

    All channel adapters (app SSE, ...) build this and hand it to a
    RecommendationPort implementation. The port owns the mapping to whatever
    backend is wired in (in-process pipeline today; HTTP call to a separate
    service tomorrow).

    Critique fields (exclude_*, boost_keywords, max_price, min_price) are
    applied Python-side post-RPC — no DB function signature change.
    """

    image_url: str
    item_label: str | None
    intent: str | None
    keywords: list[str] = field(default_factory=list)
    tolerance: float = 0.5
    color: str | None = None
    # Critique signals (from CritiqueDelta) — applied as post-RPC filters/boosts.
    exclude_brands: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_product_ids: list[str] = field(default_factory=list)
    boost_brands: list[str] = field(default_factory=list)
    boost_keywords: list[str] = field(default_factory=list)
    max_price: int | None = None
    min_price: int | None = None
    # SPEC-VISION-UNIFY-001 / REQ-VISION-SEARCH-001 — rich Vision context.
    # All optional with safe defaults so existing call sites remain unchanged.
    item_subcategory: str | None = None
    item_fit: str | None = None
    item_fabric: str | None = None
    item_color_family: str | None = None
    item_search_query_en: str | None = None
    item_search_query_ko: str | None = None
    outfit_style_node_primary: str | None = None
    outfit_style_node_secondary: str | None = None
    outfit_mood_tags: list[str] = field(default_factory=list)
    outfit_gender: str | None = None


@dataclass(frozen=True)
class ChannelRecommendationResult:
    candidates: list[Candidate]
    counts: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, int] = field(default_factory=dict)


class RecommendationPort(Protocol):
    async def recommend(self, req: ChannelRecommendationRequest) -> ChannelRecommendationResult: ...


def _build_query(
    intent: str | None,
    keywords: list[str],
    boost_keywords: list[str],
    exclude_keywords: list[str],
) -> str:
    """Compose the sparse query. Boost keywords are appended (positive signal);
    exclude keywords are NOT injected (handled by post-RPC filter)."""
    parts = [(intent or "").strip(), " ".join(keywords).strip(), " ".join(boost_keywords).strip()]
    q = " ".join(p for p in parts if p).strip().lower()
    # Exclude keywords stripped from query if present — avoid the "less denim"
    # paradox where the keyword we're trying to remove is in the search string.
    if exclude_keywords:
        excl = {e.strip().lower() for e in exclude_keywords if e.strip()}
        q = " ".join(t for t in q.split() if t not in excl)
    return q[:256]


def _apply_post_filters(candidates: list[Any], req: ChannelRecommendationRequest) -> list[Any]:
    """Apply critique filters that the RPC doesn't know about. Order:
    exclude_product_ids → exclude_brands → price range → boost reorder."""
    excl_ids = {pid for pid in req.exclude_product_ids if pid}
    excl_brands = {b.strip().lower() for b in req.exclude_brands if b and b.strip()}
    boost_brands = {b.strip().lower() for b in req.boost_brands if b and b.strip()}

    def keep(c: Any) -> bool:
        cid = str(getattr(c, "id", "") or "")
        if cid and cid in excl_ids:
            return False
        cb = (getattr(c, "brand", "") or "").strip().lower()
        if cb and cb in excl_brands:
            return False
        price = getattr(c, "price", None)
        if req.max_price is not None and isinstance(price, int) and price > req.max_price:
            return False
        if req.min_price is not None and isinstance(price, int) and price < req.min_price:
            return False
        return True

    filtered = [c for c in candidates if keep(c)]

    # Boost: stable-sort liked brands to the top while preserving relative order.
    if boost_brands:
        filtered.sort(key=lambda c: 0 if (getattr(c, "brand", "") or "").strip().lower() in boost_brands else 1)
    return filtered


class PipelineRecommendationPort:
    """In-process adapter — wraps `app.pipeline.runner.run_pipeline`.

    The pipeline import is lazy so this module stays cheap to import and the
    boundary stays explicit: scenario depends on this port, not on the
    pipeline package directly.
    """

    async def recommend(self, req: ChannelRecommendationRequest) -> ChannelRecommendationResult:
        from app.models.request import AnalyzedItem, RecommendRequest, StyleNode
        from app.pipeline.runner import run_pipeline

        composed_query = _build_query(req.intent, req.keywords, req.boost_keywords, req.exclude_keywords)
        # SPEC-VISION-UNIFY-001 / REQ-VISION-SEARCH-002 — rich fields win over
        # legacy fields when both are present.
        search_query = req.item_search_query_en or composed_query or (req.item_label or "fashion item")
        color_family = req.item_color_family or req.color or None
        item = AnalyzedItem(
            id=f"channel-{uuid.uuid4().hex[:12]}",
            category=(req.item_label or "item"),
            subcategory=req.item_subcategory,
            name=req.item_label,
            fit=req.item_fit,
            fabric=req.item_fabric,
            color_family=color_family,
            search_query=search_query,
            search_query_ko=req.item_search_query_ko,
        )
        # Pull more candidates when we know we'll filter — gives diversify a
        # better pool after exclude_product_ids / exclude_brands shrink it.
        will_filter = bool(req.exclude_product_ids or req.exclude_brands or req.max_price or req.min_price)
        style_node = None
        if req.outfit_style_node_primary:
            style_node = StyleNode(
                primary=req.outfit_style_node_primary,
                secondary=req.outfit_style_node_secondary,
            )
        rec_req = RecommendRequest(
            item=item,
            image_url=req.image_url,
            tolerance=req.tolerance,
            final_limit=30 if will_filter else None,
            gender=req.outfit_gender,
            style_node=style_node,
            mood_tags=list(req.outfit_mood_tags) if req.outfit_mood_tags else None,
        )
        resp = await run_pipeline(rec_req)
        candidates = list(resp.results) if resp and resp.results else []
        candidates = _apply_post_filters(candidates, req)
        counts = dict(getattr(resp, "counts", {}) or {})
        counts["after_critique_filter"] = len(candidates)
        latency_ms = dict(getattr(resp, "latency_ms", {}) or {})
        return ChannelRecommendationResult(
            candidates=candidates,
            counts=counts,
            latency_ms=latency_ms,
        )


_port: RecommendationPort | None = None


def get_port() -> RecommendationPort:
    global _port
    if _port is None:
        _port = PipelineRecommendationPort()
    return _port


def set_port(port: RecommendationPort) -> None:
    global _port
    _port = port


def reset_port() -> None:
    global _port
    _port = None
