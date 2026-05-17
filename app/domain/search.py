"""Search domain model (SPEC-ARCH-AI-001 PR5, REQ-AI-005).

`SearchCandidate` is the internal, framework-agnostic representation of a
post-RPC search hit. It mirrors the field-coercion semantics the runner
currently applies inline when building the transport `Candidate` DTO
(missing `score` -> 0.0, missing `brand`/`name` -> "", id -> str) so a
future caller can route through the domain layer without any behavior
change. This module is additive scaffolding: the live runner path is
unchanged, so the Net(1) RecommendResponse snapshot stays byte-identical
(REQ-AI-007).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.types import RpcRow


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    """Internal domain model for a single search result.

    Field coercions are byte-identical to app/pipeline/runner.py's inline
    Candidate construction (the Net(1)-locked behavior):
      id        -> str(row["id"])
      brand     -> row.get("brand", "")        (absent key -> "")
      name      -> row.get("name", "")
      score     -> float(row.get("score", 0.0))
      *_rank    -> row.get(...) (None passthrough)
    """

    id: str
    brand: str
    name: str
    price: int | None
    image_url: str | None
    product_url: str | None
    platform: str | None
    subcategory: str | None
    score: float
    dense_rank: int | None
    sparse_rank: int | None


def search_candidate_from_row(row: RpcRow) -> SearchCandidate:
    """Map a raw RPC row to a SearchCandidate using the EXACT coercions the
    runner applies inline today (no behavior change)."""
    return SearchCandidate(
        id=str(row["id"]),
        brand=row.get("brand", ""),
        name=row.get("name", ""),
        price=row.get("price"),
        image_url=row.get("image_url"),
        product_url=row.get("product_url"),
        platform=row.get("platform"),
        subcategory=row.get("subcategory"),
        score=float(row.get("score", 0.0)),
        dense_rank=row.get("dense_rank"),
        sparse_rank=row.get("sparse_rank"),
    )


__all__ = ["SearchCandidate", "search_candidate_from_row"]
