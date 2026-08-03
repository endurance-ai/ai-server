"""Kiko bot multi-turn blending evaluator.

Validates that the Level 2 weighted-sum blending (PR #34 / #37) preserves the
initial outfit identity while reflecting the user's modifier intent.

For each case in `multiturn_dataset.json`:
  1. Embed the `initial_query` (stand-in for an origin image vector).
  2. Embed the `modifier_query` (the user's follow-up text).
  3. Blend: `normalize(alpha * initial + (1-alpha) * modifier)`.
  4. Run `search_products_v6` with the blended vector.
  5. Score the top-k candidates:
       - identity_score   = fraction matching expected outfit_preservation
       - modifier_score   = fraction matching expected modifier_reflection
       - harmonic_score   = 2 * I * M / (I + M)

Why "initial_query as a text stand-in" instead of a real image URL:
  - Stand-in keeps the eval cheap and deterministic (no Modal image cold-start).
  - FashionSigLIP image and text live in the same 768-dim space, so weighted-
    sum behavior is faithful to the production blending path.
  - Real-image mode can be added later by swapping `embed_text(initial_query)`
    for `embed_image_url(...)`.

Baseline modes:
  - "weighted_sum"   (default)            — Level 2 basic blend (alpha-tuned)
  - "text_only"      (--mode text_only)   — modifier vector only; ignores initial
  - "image_only"     (--mode image_only)  — initial vector only; ignores modifier
  - "vector_arith"   (--mode vector_arith)— Level 2 advanced: explicit attribute
                                            swap (origin - from_attr + to_attr).
                                            Reads `expected.modifier_reflection.
                                            from_attribute` / `to_attribute` from
                                            the dataset case; skips cases that
                                            don't declare them.

Comparing the three modes shows whether the weighted-sum actually carries
identity signal that the text-only path drops.

Usage:
    export LITELLM_BASE_URL='https://litellm.kikoai.me'  # not used here, kept
                                                          # for parity with
                                                          # the guardrail script
    export MODAL_EMBED_URL='https://hchsa77--portal-embed-fastapi-app.modal.run'
    export MODAL_EMBED_TOKEN='...'
    export KIKOAI_DEVAPP_DSN='postgresql://app_user:PASS@15.165.107.28:5432/kikoai?sslmode=require'

    uv run python tests/eval/multiturn_eval.py --limit 5
    uv run python tests/eval/multiturn_eval.py --mode weighted_sum
    uv run python tests/eval/multiturn_eval.py --mode text_only --output text_only_results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

# Make the project root importable.
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("multiturn_eval")

_MODAL_TIMEOUT = 60.0
_DEFAULT_TOP_K = 10
_DEFAULT_ALPHA = 0.7


# --- Modal embedding client ------------------------------------------------


async def _embed_text(client: httpx.AsyncClient, url: str, token: str, text: str) -> list[float]:
    resp = await client.post(
        f"{url}/embed/text",
        headers={"Authorization": f"Bearer {token}"},
        json={"text": text},
        timeout=_MODAL_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    # Modal endpoint returns {"embedding": [...]} based on the production schema.
    return list(payload.get("embedding") or payload.get("vector") or [])


# --- Blending --------------------------------------------------------------


def _normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def _blend(initial: list[float], modifier: list[float], alpha: float) -> list[float]:
    blended = [alpha * a + (1.0 - alpha) * b for a, b in zip(initial, modifier, strict=True)]
    return _normalize(blended)


def _vector_arithmetic(
    initial: list[float],
    subtract: list[float],
    add: list[float],
    beta: float = 0.5,
) -> list[float]:
    """Explicit attribute swap (Level 2 advanced).

    result = normalize(initial - beta * subtract + beta * add)
    """
    result = [a - beta * s + beta * b for a, s, b in zip(initial, subtract, add, strict=True)]
    return _normalize(result)


def _build_query_vector(
    initial: list[float],
    modifier: list[float],
    mode: str,
    alpha: float,
) -> list[float]:
    if mode == "text_only":
        return modifier
    if mode == "image_only":
        return initial
    return _blend(initial, modifier, alpha)


# --- Search via direct Postgres RPC ----------------------------------------


def _embedding_to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in vec) + "]"


def _search_products_v6(
    conn: psycopg.Connection,
    *,
    query_embedding: list[float],
    top_k: int,
) -> list[dict[str, Any]]:
    """Call search_products_v6 RPC directly. Mirrors SearchRepository.search."""
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


def _match_keywords(text: str, keywords: list[str]) -> bool:
    if not text or not keywords:
        return False
    haystack = text.lower()
    return any(k.lower() in haystack for k in keywords)


def _score_results(rows: list[dict[str, Any]], expected: dict[str, Any]) -> dict[str, Any]:
    if not rows:
        return {
            "identity_score": 0.0,
            "modifier_score": 0.0,
            "harmonic_score": 0.0,
            "result_count": 0,
        }

    expected_outfit = expected.get("outfit_preservation", {})
    expected_mod = expected.get("modifier_reflection", {})

    expected_category = (expected_outfit.get("category") or "").strip().lower()
    outfit_keywords = expected_outfit.get("keywords_any") or []
    modifier_keywords = expected_mod.get("keywords_any") or []

    identity_hits = 0
    modifier_hits = 0
    samples: list[dict] = []

    for row in rows:
        name = str(row.get("name") or "")
        category = str(row.get("category") or "").strip().lower()

        # identity: category match OR garment keyword in name
        identity_match = (expected_category and category == expected_category) or _match_keywords(name, outfit_keywords)
        modifier_match = _match_keywords(name, modifier_keywords)

        if identity_match:
            identity_hits += 1
        if modifier_match:
            modifier_hits += 1

        samples.append(
            {
                "name": name[:80],
                "category": category,
                "identity_match": identity_match,
                "modifier_match": modifier_match,
            }
        )

    total = len(rows)
    identity_score = identity_hits / total
    modifier_score = modifier_hits / total
    harmonic = (
        2 * identity_score * modifier_score / (identity_score + modifier_score)
        if (identity_score + modifier_score) > 0
        else 0.0
    )

    return {
        "identity_score": round(identity_score, 3),
        "modifier_score": round(modifier_score, 3),
        "harmonic_score": round(harmonic, 3),
        "result_count": total,
        "samples": samples,
    }


# --- Aggregate -------------------------------------------------------------


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"total": 0}

    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_type[r["type"]].append(r)

    def _mean(scores: list[float]) -> float:
        return round(sum(scores) / len(scores), 3) if scores else 0.0

    overall_id = _mean([r["scores"]["identity_score"] for r in results])
    overall_mod = _mean([r["scores"]["modifier_score"] for r in results])
    overall_h = _mean([r["scores"]["harmonic_score"] for r in results])

    by_type_summary = {}
    for t, rs in by_type.items():
        by_type_summary[t] = {
            "n": len(rs),
            "identity_mean": _mean([r["scores"]["identity_score"] for r in rs]),
            "modifier_mean": _mean([r["scores"]["modifier_score"] for r in rs]),
            "harmonic_mean": _mean([r["scores"]["harmonic_score"] for r in rs]),
        }

    return {
        "total": len(results),
        "overall_identity_mean": overall_id,
        "overall_modifier_mean": overall_mod,
        "overall_harmonic_mean": overall_h,
        "by_type": by_type_summary,
    }


# --- Main ------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).with_name("multiturn_dataset.json")),
        help="평가셋 경로",
    )
    parser.add_argument(
        "--mode",
        default="weighted_sum",
        choices=["weighted_sum", "text_only", "image_only", "vector_arith"],
        help="블렌딩 모드 (default weighted_sum)",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=_DEFAULT_ALPHA,
        help=f"가중치 (default {_DEFAULT_ALPHA}, weighted_sum 모드에서만 사용)",
    )
    parser.add_argument("--top-k", type=int, default=_DEFAULT_TOP_K, help="top-K (default 10)")
    parser.add_argument(
        "--beta",
        type=float,
        default=0.5,
        help="vector_arith 모드의 속성 교체 강도 (default 0.5)",
    )
    parser.add_argument("--limit", type=int, default=None, help="처음 N개만 평가")
    parser.add_argument(
        "--output",
        default=str(Path(__file__).with_name("multiturn_results.json")),
        help="결과 저장 경로",
    )
    args = parser.parse_args()

    modal_url = os.environ.get("MODAL_EMBED_URL")
    modal_token = os.environ.get("MODAL_EMBED_TOKEN")
    dsn = os.environ.get("KIKOAI_DEVAPP_DSN")

    if not modal_url or not modal_token:
        print("ERROR: MODAL_EMBED_URL / MODAL_EMBED_TOKEN 환경변수 필요")
        sys.exit(1)
    if not dsn:
        print("ERROR: KIKOAI_DEVAPP_DSN 환경변수 필요")
        sys.exit(1)

    dataset_path = Path(args.dataset)
    data = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    if args.limit:
        cases = cases[: args.limit]

    logger.info("로드 완료: %d 케이스 · 모드=%s alpha=%.2f", len(cases), args.mode, args.alpha)

    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as http_client:
        conn = psycopg.connect(dsn, application_name="multiturn_eval")
        try:
            for i, case in enumerate(cases, 1):
                try:
                    initial_vec = await _embed_text(http_client, modal_url, modal_token, case["initial_query"])
                    if args.mode == "vector_arith":
                        attr = case.get("expected", {}).get("modifier_reflection", {})
                        from_attr = attr.get("from_attribute")
                        to_attr = attr.get("to_attribute")
                        if not from_attr or not to_attr:
                            logger.info(
                                "[%d/%d] %s [%s] SKIP — case lacks from/to attribute for vector_arith",
                                i,
                                len(cases),
                                case["id"],
                                case["type"],
                            )
                            continue
                        from_vec = await _embed_text(http_client, modal_url, modal_token, from_attr)
                        to_vec = await _embed_text(http_client, modal_url, modal_token, to_attr)
                        query_vec = _vector_arithmetic(initial_vec, from_vec, to_vec, beta=args.beta)
                    else:
                        modifier_vec = await _embed_text(http_client, modal_url, modal_token, case["modifier_query"])
                        query_vec = _build_query_vector(initial_vec, modifier_vec, args.mode, args.alpha)
                    rows = _search_products_v6(conn, query_embedding=query_vec, top_k=args.top_k)
                    scores = _score_results(rows, case.get("expected", {}))
                    result = {
                        "id": case["id"],
                        "type": case["type"],
                        "initial_query": case["initial_query"],
                        "modifier_query": case["modifier_query"],
                        "scores": scores,
                    }
                    results.append(result)
                    logger.info(
                        "[%d/%d] %s [%s] id=%.2f mod=%.2f H=%.2f n=%d",
                        i,
                        len(cases),
                        case["id"],
                        case["type"],
                        scores["identity_score"],
                        scores["modifier_score"],
                        scores["harmonic_score"],
                        scores["result_count"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[%s] failed: %r", case["id"], exc)
                    results.append(
                        {
                            "id": case["id"],
                            "type": case["type"],
                            "error": f"{type(exc).__name__}: {exc}",
                            "scores": {
                                "identity_score": 0.0,
                                "modifier_score": 0.0,
                                "harmonic_score": 0.0,
                                "result_count": 0,
                            },
                        }
                    )
        finally:
            conn.close()

    summary = _aggregate(results)

    print()
    print("=" * 60)
    print(f"📊 Multi-turn Blending Evaluation ({args.mode}, alpha={args.alpha})")
    print("=" * 60)
    print(f"  Total              : {summary['total']}")
    print(f"  Identity mean      : {summary['overall_identity_mean']}")
    print(f"  Modifier mean      : {summary['overall_modifier_mean']}")
    print(f"  Harmonic mean      : {summary['overall_harmonic_mean']}")
    print()
    print("  By scenario type:")
    for t, v in summary["by_type"].items():
        print(
            f"    {t:<24}: n={v['n']} "
            f"identity={v['identity_mean']} "
            f"modifier={v['modifier_mean']} "
            f"harmonic={v['harmonic_mean']}"
        )
    print("=" * 60)

    Path(args.output).write_text(
        json.dumps(
            {"mode": args.mode, "alpha": args.alpha, "summary": summary, "results": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("결과 저장: %s", args.output)


if __name__ == "__main__":
    asyncio.run(main())
