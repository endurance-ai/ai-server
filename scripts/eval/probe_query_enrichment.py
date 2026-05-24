"""V4 query-enrichment experiment: sparse 5-slot vs enriched (Vision `detail`).

Vision already extracts rich per-item attributes (`detail` = neckline/silhouette/
construction, length, pattern) but `searchQuery` collapses to a fixed 5-slot
template "[fit] [color] [fabric] [subcategory] [gender]" and DISCARDS them.
Marqo-FashionSigLIP was GCL-trained on colors/materials/fine-details and is
robust to long queries — so the discarded detail SHOULD help cross-modal recall.

This probe runs each item as (sparse, enriched) through the real text path and
compares top-1/median distance + top-3 product names so we can judge whether
enrichment pulls genuinely-more-relevant products up (or just over-constrains).

Run:  uv run python scripts/eval/probe_query_enrichment.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# (label, sparse 5-slot query, enriched query w/ Vision `detail` attributes)
PAIRS: list[tuple[str, str, str]] = [
    (
        "logo shirt",
        "oversized white cotton logo shirt men",
        "oversized white cotton crew neck short sleeve front logo graphic print shirt men",
    ),
    (
        "denim shorts",
        "slim navy denim shorts men",
        "slim navy blue denim five pocket knee length frayed hem shorts men",
    ),
    ("loafers", "black leather loafers men", "black leather penny loafers slip on round toe men"),
    ("wool coat", "beige wool coat women", "oversized beige wool double breasted notch lapel knee length coat women"),
    ("floral dress", "floral midi dress women", "floral print v neck puff sleeve a-line chiffon midi dress women"),
    ("knit sweater", "grey wool sweater unisex", "grey wool ribbed crew neck chunky knit drop shoulder sweater unisex"),
    (
        "denim jacket",
        "blue denim jacket men",
        "washed light blue denim trucker jacket button front chest pockets boxy men",
    ),
    ("slip dress", "black slip dress women", "black satin spaghetti strap cowl neck bias cut slip midi dress women"),
]


async def _run(query: str) -> dict:
    from app.infrastructure.repositories.search_repository import SearchRepository
    from app.pipeline.embed import EmbedProvider

    emb = await EmbedProvider.embed_text(query)
    rows = await SearchRepository.search(SearchRepository.build_params(embedding=emb, brand_filter=None, category=None))
    s = sorted(float(r.get("distance", 1.0)) for r in rows)
    names = [f"{r.get('brand', '')} {r.get('name', '')}".strip()[:42] for r in rows[:3]]
    return {"min": s[0], "median": statistics.median(s), "top3": names}


async def main() -> None:
    print(f"{'item':14s} {'variant':9s} {'min':>7s} {'median':>7s}  top-3")
    print("-" * 110)
    for label, sparse, enriched in PAIRS:
        rs = await _run(sparse)
        re = await _run(enriched)
        d_med = re["median"] - rs["median"]
        d_min = re["min"] - rs["min"]
        print(f"{label:14s} {'sparse':9s} {rs['min']:7.4f} {rs['median']:7.4f}  {' | '.join(rs['top3'])}")
        print(f"{label:14s} {'enriched':9s} {re['min']:7.4f} {re['median']:7.4f}  {' | '.join(re['top3'])}")
        arrow_med = "↓better" if d_med < 0 else "↑worse"
        arrow_min = "↓better" if d_min < 0 else "↑worse"
        print(f"{'':14s} {'Δ':9s} min {d_min:+.4f}({arrow_min}) median {d_med:+.4f}({arrow_med})")
        print()


if __name__ == "__main__":
    asyncio.run(main())
