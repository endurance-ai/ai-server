"""Kiko search-quality evaluator.

Runs each case in `search_quality_dataset.json` through the real production
retrieval path (Modal `/embed/text` → dev-app Postgres `search_products_v6`
RPC) and scores the top-K against attribute-based ground truth. The point of
this harness is to make search-quality changes *measurable* — swap in a new
prompt / rewriter / filter, re-run, and diff the numbers instead of eyeballing
QA traces.

Output includes the current git SHA and a timestamp so two runs can be
compared later with `compare.py`.

Environment:
    MODAL_EMBED_URL / MODAL_EMBED_TOKEN     — required (Modal embed endpoint)
    KIKOAI_DEVAPP_DSN or DB_DSN             — required (direct Postgres DSN;
                                              PostgREST is bypassed on purpose
                                              so we test the raw retrieval,
                                              not the shim.)

Usage:
    uv run python tests/eval/search_quality_eval.py
    uv run python tests/eval/search_quality_eval.py --top-k 5 --limit 10
    uv run python tests/eval/search_quality_eval.py --output custom.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("search_quality_eval")

_MODAL_TIMEOUT = 60.0
_DEFAULT_TOP_K = 5


# --- Modal embedding -------------------------------------------------------


async def _embed_text(client: httpx.AsyncClient, url: str, token: str, text: str) -> list[float]:
    resp = await client.post(
        f"{url}/embed/text",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": text},
        timeout=_MODAL_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    return list(payload.get("embedding") or payload.get("vector") or [])


# --- Postgres RPC ---------------------------------------------------------


def _embedding_to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


def _search_products_v6(
    conn: psycopg.Connection,
    *,
    query_embedding: list[float],
    top_k: int,
) -> list[dict[str, Any]]:
    """Direct RPC call — same signature as SearchRepository.search but bypasses
    PostgREST shim. Mirrors `tests/eval/multiturn_eval.py` for consistency."""
    sql = """
        SELECT * FROM search_products_v6(
            query_embedding := %s::halfvec,
            p_style_node_id := NULL,
            p_category := 'other',
            p_subcategory := NULL,
            p_brand_names := NULL,
            p_limit := %s
        )
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, [_embedding_to_pgvector(query_embedding), top_k])
        return list(cur.fetchall())


# --- Scoring ---------------------------------------------------------------


def _match_any(text: str, needles: list[str]) -> bool:
    """Case-insensitive substring match. Empty needle list → False."""
    if not text or not needles:
        return False
    haystack = text.lower()
    return any(n.lower() in haystack for n in needles)


def _score_case(rows: list[dict[str, Any]], expected: dict[str, Any]) -> dict[str, Any]:
    """Metrics per case (all @top-K where K = len(rows)).

    PRIMARY
    - quality_score : keyword_hit — single-number summary the compare tool
                      grades on. subcategory intentionally excluded because
                      52% of prod rows have NULL subcategory (crawler gap),
                      which caps a sqrt(subcat*keyword) primary at the tagging
                      rate rather than at retrieval quality. Once the crawler
                      backfills we can re-promote subcat to primary.

    RETRIEVAL SIGNALS
    - keyword_hit   : fraction whose name/brand matches expected.keywords_any
                      (reliable — name/brand ≈ 100% coverage)
    - color_hit     : fraction whose name matches expected.color_any (or None if not asserted)
    - fit_hit       : fraction whose name matches expected.fit_any (or None if not asserted)
    - brand_diversity : count of unique brands
    - distance_p50  : median distance (lower = more confident retrieval)

    DIAGNOSTIC (visible but not scored on)
    - subcat_hit         : fraction whose subcategory ∈ expected.subcategory_any
                           (NULL rows count as miss — reflects tag coverage AND accuracy)
    - subcat_hit_tagged  : subcat_hit computed ONLY over rows with non-NULL
                           subcategory (real tagging accuracy). None when
                           tag_coverage is 0.
    - tag_coverage       : fraction of top-K with a non-NULL subcategory
                           (crawler tagging rate for this retrieval slice)
    """
    if not rows:
        return {
            "quality_score": 0.0,
            "keyword_hit": 0.0,
            "color_hit": None,
            "fit_hit": None,
            "brand_hit": None,
            "brand_diversity": 0,
            "distance_p50": None,
            "subcat_hit": 0.0,
            "subcat_hit_tagged": None,
            "tag_coverage": 0.0,
            "result_count": 0,
            "samples": [],
        }

    subcat_expected = [s.lower() for s in (expected.get("subcategory_any") or [])]
    keywords = expected.get("keywords_any") or []
    colors = expected.get("color_any") or []
    fits = expected.get("fit_any") or []
    brand_hints = expected.get("brand_any") or []

    subcat_hits = 0
    subcat_hits_tagged = 0
    tagged_count = 0
    keyword_hits = 0
    color_hits = 0
    fit_hits = 0
    brand_hits = 0
    brands: set[str] = set()
    distances: list[float] = []
    samples: list[dict[str, Any]] = []

    for row in rows:
        name = str(row.get("name") or "")
        brand = str(row.get("brand") or "").strip()
        raw_subcat = row.get("subcategory")
        subcategory = str(raw_subcat or "").strip().lower()
        is_tagged = raw_subcat is not None and subcategory != ""
        distance = row.get("distance")

        # subcategory scoring: prefix or exact match against any expected token.
        # NULL/empty subcategory always misses subcat_hit (reflects tag coverage).
        sc_match = (
            is_tagged
            and bool(subcat_expected)
            and any(
                (subcategory == token) or subcategory.startswith(token) or token in subcategory
                for token in subcat_expected
            )
        )
        # keyword scoring: look in name OR brand (some products encode garment
        # type in the name, others rely on the brand line).
        kw_match = _match_any(name, keywords) or _match_any(brand, keywords)
        color_match = _match_any(name, colors) if colors else None
        fit_match = _match_any(name, fits) if fits else None
        brand_match = _match_any(brand, brand_hints) if brand_hints else None

        if is_tagged:
            tagged_count += 1
            if sc_match:
                subcat_hits_tagged += 1
        if sc_match:
            subcat_hits += 1
        if kw_match:
            keyword_hits += 1
        if color_match:
            color_hits += 1
        if fit_match:
            fit_hits += 1
        if brand_match:
            brand_hits += 1
        if brand:
            brands.add(brand.lower())
        if isinstance(distance, (int, float)):
            distances.append(float(distance))

        samples.append(
            {
                "id": row.get("id"),
                "brand": brand,
                "name": name[:80],
                "subcategory": subcategory,
                "distance": round(distance, 4) if isinstance(distance, (int, float)) else None,
                "sc": sc_match,
                "kw": kw_match,
                "co": color_match,
                "fi": fit_match,
            }
        )

    n = len(rows)
    subcat_hit = subcat_hits / n
    keyword_hit = keyword_hits / n
    # PRIMARY: keyword_hit only. subcategory kept as diagnostic because the
    # 52% NULL rate on products.subcategory (crawler backfill in progress)
    # would make sqrt(subcat*keyword) grade the crawler tagging rate, not
    # retrieval quality. Once tag_coverage climbs closer to 1.0 across the
    # dataset we can re-promote subcat into the primary formula.
    quality = keyword_hit

    return {
        "quality_score": round(quality, 3),
        "keyword_hit": round(keyword_hit, 3),
        "color_hit": round(color_hits / n, 3) if colors else None,
        "fit_hit": round(fit_hits / n, 3) if fits else None,
        "brand_hit": round(brand_hits / n, 3) if brand_hints else None,
        "brand_diversity": len(brands),
        "distance_p50": round(statistics.median(distances), 4) if distances else None,
        # diagnostic — visible in results but not graded on
        "subcat_hit": round(subcat_hit, 3),
        "subcat_hit_tagged": round(subcat_hits_tagged / tagged_count, 3) if tagged_count else None,
        "tag_coverage": round(tagged_count / n, 3),
        "result_count": n,
        "samples": samples,
    }


# --- Aggregation -----------------------------------------------------------


def _mean_optional(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 3)


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"total": 0}
    valid = [r for r in results if "scores" in r]

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in valid:
        by_type[r["type"]].append(r)

    def _agg(rs: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "n": len(rs),
            "quality_score_mean": _mean_optional([r["scores"]["quality_score"] for r in rs]),
            "keyword_hit_mean": _mean_optional([r["scores"]["keyword_hit"] for r in rs]),
            "color_hit_mean": _mean_optional([r["scores"]["color_hit"] for r in rs]),
            "fit_hit_mean": _mean_optional([r["scores"]["fit_hit"] for r in rs]),
            "brand_diversity_mean": _mean_optional([r["scores"]["brand_diversity"] for r in rs]),
            "distance_p50_mean": _mean_optional([r["scores"]["distance_p50"] for r in rs]),
            # diagnostic
            "subcat_hit_mean": _mean_optional([r["scores"]["subcat_hit"] for r in rs]),
            "subcat_hit_tagged_mean": _mean_optional([r["scores"]["subcat_hit_tagged"] for r in rs]),
            "tag_coverage_mean": _mean_optional([r["scores"]["tag_coverage"] for r in rs]),
        }

    return {
        "total": len(valid),
        "errors": len(results) - len(valid),
        "overall": _agg(valid),
        "by_type": {t: _agg(rs) for t, rs in by_type.items()},
    }


# --- git sha for reproducibility -------------------------------------------


def _git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short=8", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return "nogit"


def _git_branch() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return "unknown"


# --- Main ------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).with_name("search_quality_dataset.json")),
        help="골든 세트 경로",
    )
    parser.add_argument("--top-k", type=int, default=_DEFAULT_TOP_K, help="top-K (default 5)")
    parser.add_argument("--limit", type=int, default=None, help="처음 N개만 실행")
    parser.add_argument("--label", default=None, help="결과 파일 라벨 (예: 'baseline' / 'with_rewrite')")
    parser.add_argument("--output", default=None, help="결과 저장 경로 (기본: results/search_quality_<sha>_<ts>.json)")
    args = parser.parse_args()

    modal_url = os.environ.get("MODAL_EMBED_URL")
    modal_token = os.environ.get("MODAL_EMBED_TOKEN")
    dsn = os.environ.get("KIKOAI_DEVAPP_DSN") or os.environ.get("DB_DSN")

    if not modal_url or not modal_token:
        print("ERROR: MODAL_EMBED_URL / MODAL_EMBED_TOKEN 환경변수 필요")
        sys.exit(1)
    if not dsn:
        print("ERROR: KIKOAI_DEVAPP_DSN 또는 DB_DSN 환경변수 필요")
        sys.exit(1)

    dataset_path = Path(args.dataset)
    data = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    if args.limit:
        cases = cases[: args.limit]

    sha = _git_sha()
    branch = _git_branch()
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    label_part = f"_{args.label}" if args.label else ""

    if args.output:
        output_path = Path(args.output)
    else:
        output_dir = Path(__file__).with_name("results")
        output_dir.mkdir(exist_ok=True)
        output_path = output_dir / f"search_quality_{sha}{label_part}_{ts}.json"

    logger.info("로드 완료: %d 케이스 · top_k=%d · sha=%s · branch=%s", len(cases), args.top_k, sha, branch)

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as http_client:
        conn = psycopg.connect(dsn, application_name="search_quality_eval")
        try:
            for i, case in enumerate(cases, 1):
                try:
                    query_vec = await _embed_text(http_client, modal_url, modal_token, case["input"])
                    rows = _search_products_v6(conn, query_embedding=query_vec, top_k=args.top_k)
                    scores = _score_case(rows, case.get("expected", {}))
                    results.append(
                        {
                            "id": case["id"],
                            "type": case["type"],
                            "input": case["input"],
                            "lang": case.get("lang"),
                            "scores": scores,
                        }
                    )
                    logger.info(
                        "[%d/%d] %s [%s] sc=%.2f kw=%.2f Q=%.2f n=%d",
                        i,
                        len(cases),
                        case["id"],
                        case["type"],
                        scores["subcat_hit"],
                        scores["keyword_hit"],
                        scores["quality_score"],
                        scores["result_count"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] failed: %r", case["id"], exc)
                    results.append(
                        {
                            "id": case["id"],
                            "type": case["type"],
                            "input": case.get("input"),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        finally:
            conn.close()

    summary = _aggregate(results)

    print()
    print("=" * 66)
    print(f"📊 Search Quality Eval — sha={sha} branch={branch} label={args.label or '-'}")
    print("=" * 66)
    print(f"  Total              : {summary['total']} (errors: {summary.get('errors', 0)})")
    ov = summary["overall"]
    print(f"  Quality mean       : {ov['quality_score_mean']}    ← primary (= keyword_hit)")
    print(f"  Keyword hit mean   : {ov['keyword_hit_mean']}")
    print(f"  Color hit mean     : {ov['color_hit_mean']}    (None = not asserted)")
    print(f"  Fit hit mean       : {ov['fit_hit_mean']}    (None = not asserted)")
    print(f"  Brand diversity avg: {ov['brand_diversity_mean']}")
    print(f"  Distance p50 avg   : {ov['distance_p50_mean']}    (lower = better)")
    print("  ─── diagnostic (not graded) ───")
    print(f"  Tag coverage mean  : {ov['tag_coverage_mean']}    ← crawler subcategory fill rate")
    print(f"  Subcat hit mean    : {ov['subcat_hit_mean']}    (all rows, NULL counted as miss)")
    print(f"  Subcat hit (tagged): {ov['subcat_hit_tagged_mean']}    (real tag accuracy on non-NULL rows)")
    print()
    print("  By type:")
    for t, v in summary["by_type"].items():
        print(
            f"    {t:<22} n={v['n']:<3} "
            f"Q={v['quality_score_mean']} "
            f"sc={v['subcat_hit_mean']} "
            f"kw={v['keyword_hit_mean']} "
            f"dist={v['distance_p50_mean']}"
        )
    print("=" * 66)
    print(f"  Results: {output_path}")

    output_path.write_text(
        json.dumps(
            {
                "meta": {
                    "sha": sha,
                    "branch": branch,
                    "label": args.label,
                    "top_k": args.top_k,
                    "timestamp": ts,
                    "dataset_version": data.get("version"),
                },
                "summary": summary,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    asyncio.run(main())
