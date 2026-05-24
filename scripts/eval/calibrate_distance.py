"""V4-A distance-signal calibration harness (SPEC-SEARCH-QUALITY-SIGNAL-001 precursor).

Drives the EXACT production text-only path (embed_text → build_params →
SearchRepository.search → raw top-50 rows) for a curated set of good /
borderline / bad queries and reports the cross-modal cosine distance
distribution per query and aggregated per bucket.

WHY: v6 is cross-modal (text→image SigLIP). The usable distance range is
compressed (~0.87–0.96); a perfect match bottoms out at ~0.88, nonsense
spreads to median ~0.96. `min` alone is barely discriminative. This harness
collects the empirical good-vs-bad distribution so the weak-result判定式
(median + spread) can be calibrated against THIS catalog — never an absolute
literature threshold (the RAG "drop > 0.3" rule does not transfer; see memory
project-search-distance-calibration).

Run:  uv run python -m scripts.eval.calibrate_distance
Out:  stdout markdown table + /tmp/v4a_distance_calibration.json

This is a reusable retuning tool: re-run it whenever the catalog grows or the
embedding model changes, then re-derive the env thresholds.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# Project root on sys.path (mirror scripts/eval/run.py) so `import app.*` works
# when invoked as a plain file (`uv run python scripts/eval/calibrate_distance.py`).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Curated calibration set. Buckets are the GROUND TRUTH labels we calibrate
# against — chosen to span the spectrum the live agent actually sees.
#   good       — well-formed, in-catalog fashion intents (should match well)
#   borderline — plausible but niche / catalog-thin (the hard middle)
#   bad        — nonsense / non-fashion / empty-semantic (should look weak)
# Gender token is included where a real agent query would carry one (the
# system pins gender downstream); category gate is skipped on the text path
# (category=None → `other`), matching production text-only search exactly.
QUERIES: dict[str, list[str]] = {
    "good": [
        "black oversized hoodie men",
        "white leather sneakers unisex",
        "beige wool coat women",
        "blue denim jacket men",
        "grey wool knit sweater unisex",
        "black leather chelsea boots men",
        "floral midi dress women",
        "striped cotton t-shirt men",
        "black leather tote bag women",
        "navy chino trousers men",
        "cream cable knit cardigan women",
        "olive green cargo pants men",
        "pleated mini skirt women",
        "camel trench coat women",
        "white oxford button down shirt men",
        "black slip dress women",
    ],
    "borderline": [
        "lavender leather cropped capri pants women",
        "neon orange faux fur coat women",
        "sequin blazer men",
        "tie dye bucket hat unisex",
        "victorian corset top women",
        "metallic silver cargo skirt women",
        "patchwork denim overalls unisex",
        "holographic puffer jacket men",
        "fringe suede western vest men",
        "checkered flared trousers women",
    ],
    "bad": [
        "quantum banana telescope error",
        "asdfghjkl qwerty zxcvbn",
        "the weather today is quite nice outside",
        "calculus homework help integral derivative",
        "12345 67890 00000 99999",
        "how do i reset my email password",
        "lorem ipsum dolor sit amet consectetur",
        "stock market interest rate federal reserve",
    ],
}


def _stats(distances: list[float]) -> dict[str, float | int]:
    """Distribution metrics over a raw RPC distance list (ASC = better)."""
    s = sorted(distances)
    n = len(s)
    if n == 0:
        return {"n": 0}

    def _pct(p: float) -> float:
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return s[idx]

    mn = s[0]
    median = statistics.median(s)
    p25 = _pct(0.25)
    p75 = _pct(0.75)
    p90 = _pct(0.90)
    return {
        "n": n,
        "min": round(mn, 4),
        "p25": round(p25, 4),
        "median": round(median, 4),
        "p75": round(p75, 4),
        "p90": round(p90, 4),
        "max": round(s[-1], 4),
        "mean": round(statistics.fmean(s), 4),
        "std": round(statistics.pstdev(s), 4),
        # Candidate weak-signal features (relative, not absolute):
        "spread_med_min": round(median - mn, 4),  # tightness of the head
        "spread_p90_min": round(p90 - mn, 4),  # head-to-tail spread
    }


async def _probe(query: str) -> dict[str, Any]:
    """Embed → raw v6 RPC (no diversify) → distance stats over top-50."""
    from app.infrastructure.repositories.search_repository import SearchRepository
    from app.pipeline.embed import EmbedProvider

    t0 = time.perf_counter()
    embedding = await EmbedProvider.embed_text(query)
    embed_ms = int((time.perf_counter() - t0) * 1000)

    # Faithful text-only path: category=None → to_canonical_family → "other"
    # → family gate skipped (pure cosine), no brand filter.
    params = SearchRepository.build_params(embedding=embedding, brand_filter=None, category=None)
    t1 = time.perf_counter()
    rows = await SearchRepository.search(params)
    rpc_ms = int((time.perf_counter() - t1) * 1000)

    distances = [float(r.get("distance", 1.0)) for r in rows]
    degraded = sum(1 for r in rows if r.get("degraded"))
    top1 = rows[0] if rows else {}
    return {
        "query": query,
        "embed_ms": embed_ms,
        "rpc_ms": rpc_ms,
        "degraded_count": degraded,
        "top1": f"{top1.get('brand', '')} {top1.get('name', '')}".strip()[:60],
        **_stats(distances),
    }


async def main() -> None:
    results: dict[str, list[dict[str, Any]]] = {}
    for bucket, queries in QUERIES.items():
        results[bucket] = []
        for q in queries:
            try:
                r = await _probe(q)
            except Exception as exc:  # noqa: BLE001 — record + continue
                r = {"query": q, "error": f"{type(exc).__name__}: {exc}"}
            results[bucket].append(r)
            line = (
                f"[{bucket:10s}] min={r.get('min')} med={r.get('median')} "
                f"p90={r.get('p90')} spread(m-min)={r.get('spread_med_min')} "
                f"spread(p90-min)={r.get('spread_p90_min')} deg={r.get('degraded_count')} "
                f"n={r.get('n')} :: {q[:48]}"
            )
            if r.get("error"):
                line = f"[{bucket:10s}] ERROR {r['error']} :: {q[:48]}"
            print(line, flush=True)

    # Per-bucket aggregates over the discriminating features.
    print("\n=== BUCKET AGGREGATES (mean across queries) ===", flush=True)
    agg: dict[str, dict[str, float]] = {}
    for bucket, rows in results.items():
        ok = [r for r in rows if not r.get("error") and r.get("n")]
        if not ok:
            continue

        def _m(key: str) -> float:
            return round(statistics.fmean([float(r[key]) for r in ok]), 4)

        agg[bucket] = {
            "count": len(ok),
            "min": _m("min"),
            "median": _m("median"),
            "p90": _m("p90"),
            "spread_med_min": _m("spread_med_min"),
            "spread_p90_min": _m("spread_p90_min"),
            "degraded_count": _m("degraded_count"),
        }
        a = agg[bucket]
        print(
            f"{bucket:10s} (n={a['count']:2d}) "
            f"min={a['min']} median={a['median']} p90={a['p90']} "
            f"spread(m-min)={a['spread_med_min']} spread(p90-min)={a['spread_p90_min']} "
            f"deg={a['degraded_count']}",
            flush=True,
        )

    out = {"results": results, "aggregates": agg, "queries": QUERIES}
    with open("/tmp/v4a_distance_calibration.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("\nwrote /tmp/v4a_distance_calibration.json", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
