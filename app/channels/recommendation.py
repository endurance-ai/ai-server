"""Recommendation port — channel-side abstraction over the search pipeline.

Decouples `app.channels.scenario` from `app.pipeline.runner` so the messenger
flow can later be split into its own process / repo without rewriting scenario
logic. Channels speak in `ChannelRecommendationRequest` (intent + keywords +
image), not the REST DTO `RecommendRequest`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

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

    All channel adapters (Telegram, iMessage, ...) build this and hand it to a
    RecommendationPort implementation. The port owns the mapping to whatever
    backend is wired in (in-process pipeline today; HTTP call to a separate
    service tomorrow).
    """

    image_url: str
    item_label: str | None
    intent: str | None
    keywords: list[str] = field(default_factory=list)
    tolerance: float = 0.5
    color: str | None = None


@dataclass(frozen=True)
class ChannelRecommendationResult:
    candidates: list[Candidate]
    counts: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, int] = field(default_factory=dict)


class RecommendationPort(Protocol):
    async def recommend(self, req: ChannelRecommendationRequest) -> ChannelRecommendationResult: ...


def _build_query(intent: str | None, keywords: list[str]) -> str:
    parts = [(intent or "").strip(), " ".join(keywords).strip()]
    q = " ".join(p for p in parts if p).strip().lower()
    return q[:256]


class PipelineRecommendationPort:
    """In-process adapter — wraps `app.pipeline.runner.run_pipeline`.

    The pipeline import is lazy so this module stays cheap to import and the
    boundary stays explicit: scenario depends on this port, not on the
    pipeline package directly.
    """

    async def recommend(self, req: ChannelRecommendationRequest) -> ChannelRecommendationResult:
        from app.models.request import AnalyzedItem, RecommendRequest
        from app.pipeline.runner import run_pipeline

        query = _build_query(req.intent, req.keywords)
        item = AnalyzedItem(
            id=f"channel-{uuid.uuid4().hex[:12]}",
            category=(req.item_label or "item"),
            subcategory=None,
            name=req.item_label,
            color_family=req.color or None,
            search_query=query or (req.item_label or "fashion item"),
            search_query_ko=None,
        )
        rec_req = RecommendRequest(
            item=item,
            image_url=req.image_url,
            tolerance=req.tolerance,
        )
        resp = await run_pipeline(rec_req)
        candidates = list(resp.results) if resp and resp.results else []
        counts = dict(getattr(resp, "counts", {}) or {})
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
