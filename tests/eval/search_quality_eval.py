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
    LITELLM_BASE_URL / LITELLM_MASTER_KEY   — required with --rewrite (LLM
                                              call for KO → EN rewrite)

Usage:
    # A. Raw baseline (input goes straight to embedder — measures pure
    #    retrieval quality at the FashionSigLIP level, isolates the LLM
    #    rewrite step)
    uv run python tests/eval/search_quality_eval.py --label raw_baseline

    # B. Prod baseline (KO cases first go through an LLM rewrite to
    #    concise English keywords — mirrors the ReAct agent's behavior
    #    before it calls the search_products tool)
    uv run python tests/eval/search_quality_eval.py --rewrite --label prod_baseline
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


# --- Feature-metadata attribute maps ("우와 비슷하다" — 구조화 속성 채점) ---
# products.product_features.feature_metadata 는 VLM 추출 구조화 속성(핏/색/넥라인/
# 패턴, 커버리지 사실상 완전)이다. color/fit 을 상품 NAME substring 이 아니라 이
# 속성으로 채점하면 "결과가 실제로 그 핏/색인가"를 정확히 잰다 (legacy name-proxy
# 는 이름에 색·핏이 적힌 상품에서만 맞아 저평가된다).
_COLOR_TOKEN_TO_FAMILY: dict[str, set[str]] = {
    "black": {"BLACK"},
    "블랙": {"BLACK"},
    "white": {"WHITE"},
    "화이트": {"WHITE"},
    "ivory": {"CREAM", "WHITE"},
    "cream": {"CREAM"},
    "beige": {"BEIGE", "CREAM"},
    "베이지": {"BEIGE", "CREAM"},
    "camel": {"BEIGE", "BROWN", "KHAKI"},
    "tan": {"BEIGE", "BROWN", "KHAKI"},
    "khaki": {"KHAKI"},
    "grey": {"GREY"},
    "gray": {"GREY"},
    "그레이": {"GREY"},
    "blue": {"BLUE", "NAVY"},
    "baby blue": {"BLUE"},
    "navy": {"NAVY"},
    "brown": {"BROWN"},
    "브라운": {"BROWN"},
    "cognac": {"BROWN"},
    "chocolate": {"BROWN"},
    "green": {"GREEN"},
    "red": {"RED"},
    "yellow": {"YELLOW"},
    "orange": {"ORANGE"},
    "pink": {"PINK"},
    "lavender": {"PURPLE"},
    "purple": {"PURPLE"},
    # 의도적으로 미매핑(너무 모호해 채점 제외): "pastel", "multi"
}
_FIT_TOKEN_TO_VALUE: dict[str, set[str]] = {
    "oversized": {"oversized"},
    "loose": {"relaxed", "oversized"},
    "relaxed": {"relaxed"},
    "boxy": {"oversized", "relaxed"},
    "drop-shoulder": {"oversized"},
    "wide": {"relaxed"},
    "wide-leg": {"relaxed"},
    "wideleg": {"relaxed"},
    "slim": {"slim", "skinny"},
    "skinny": {"skinny"},
    "cropped": {"cropped"},
    "longline": {"longline"},
    "regular": {"regular"},
}


def _expected_color_families(color_any: list[str]) -> set[str]:
    fams: set[str] = set()
    for tok in color_any or []:
        fams |= _COLOR_TOKEN_TO_FAMILY.get(tok.strip().lower(), set())
    return fams


def _expected_fit_values(fit_any: list[str]) -> set[str]:
    vals: set[str] = set()
    for tok in fit_any or []:
        vals |= _FIT_TOKEN_TO_VALUE.get(tok.strip().lower(), set())
    return vals


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


# --- LLM rewrite (KO → EN keywords) ---------------------------------------

# Focused rewrite prompt. NOT the full ReAct agent prompt — we only want the
# rewrite behavior the agent exhibits when it produces `text_query` for the
# search_products tool. Prod agent prompt says (paraphrased): "For text
# requests, pass a concise ENGLISH text_query (e.g. 'leather loafers')".
# Reproducing just that single step here is deterministic (temp=0), cheap
# (one short call per case), and testable.
_REWRITE_SYSTEM_PROMPT = (
    "You rewrite Korean fashion search queries to concise English keywords for a "
    "FashionSigLIP embedding retrieval system. Rules:\n"
    "- Output ONLY the rewritten English query, no explanation, no quotes.\n"
    "- Keep it short (2-6 words). Include garment type, color, silhouette/fit, "
    "and material when present.\n"
    "- If the input is already English, return it unchanged.\n"
    "- Do not invent attributes that are not in the input."
)

_REWRITE_MODEL = "claude-haiku-4-5"  # matches AGENT_LLM_MODEL default (2026-08-26 Claude 통일)


async def _rewrite_query(
    client: httpx.AsyncClient,
    litellm_url: str,
    litellm_key: str,
    query: str,
    lang: str | None,
) -> str:
    """KO fashion query → concise EN keywords via LiteLLM.

    English inputs pass through untouched. Any failure (HTTP, parse, empty)
    falls back to the original query — fail-open matches the prod agent
    behavior when rewrite is unreliable.
    """
    if not query or (lang or "").lower() == "en":
        return query
    try:
        headers = {"Content-Type": "application/json"}
        if litellm_key:
            headers["Authorization"] = f"Bearer {litellm_key}"
        resp = await client.post(
            f"{litellm_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json={
                "model": _REWRITE_MODEL,
                "messages": [
                    {"role": "system", "content": _REWRITE_SYSTEM_PROMPT},
                    {"role": "user", "content": query},
                ],
                "temperature": 0.0,
                "max_tokens": 40,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        rewritten = (content or "").strip().strip('"').strip("'")
        return rewritten or query
    except Exception as exc:  # noqa: BLE001 — fail-open, log for visibility
        logger.warning("rewrite fallback for %r: %r", query[:40], exc)
        return query


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


def _attach_feature_metadata(conn: psycopg.Connection, rows: list[dict[str, Any]]) -> None:
    """결과 id 들의 product_features.feature_metadata 를 배치 조회해 row['fmeta']
    로 붙인다. 구조화 속성 채점의 ground-truth 단일 출처."""
    ids = [r["id"] for r in rows if r.get("id") is not None]
    if not ids:
        return
    by_id: dict[Any, dict[str, Any]] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT product_id, feature_metadata FROM product_features WHERE product_id = ANY(%s)",
            [ids],
        )
        for r in cur.fetchall():
            by_id[r["product_id"]] = r["feature_metadata"] or {}
    for row in rows:
        row["fmeta"] = by_id.get(row.get("id"))


# --- Attribute-alignment rerank (검증용 — 실 파이프라인 적용 전 리프트 측정) --

_ATTR_FIT_W = 0.20
_ATTR_COLOR_W = 0.12


def _attr_align_rerank(rows: list[dict[str, Any]], expected: dict[str, Any]) -> list[dict[str, Any]]:
    """pool 을 (1−distance) + fit/color 정렬 boost 로 재정렬한다. 후보 feature_metadata
    가 쿼리 target(expected color/fit)과 맞으면 위로 끌어올려, 임베딩이 놓친 정확
    속성 매치를 visible top-K 안으로 넣는다 — "우와 비슷하다"의 핵심 배선.
    실 파이프라인의 personalize_rerank 가산 항과 동일 원리."""
    exp_fams = _expected_color_families(expected.get("color_any") or [])
    exp_fit = _expected_fit_values(expected.get("fit_any") or [])
    if not exp_fams and not exp_fit:
        return rows

    def _s(r: dict[str, Any]) -> float:
        base = 1.0 - float(r.get("distance", 1.0))
        fm = r.get("fmeta") or {}
        if exp_fams and str(fm.get("primary_color") or "").strip().upper() in exp_fams:
            base += _ATTR_COLOR_W
        if exp_fit and str(fm.get("fit") or "").strip().lower() in exp_fit:
            base += _ATTR_FIT_W
        return base

    return sorted(rows, key=_s, reverse=True)


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
            "color_feat_hit": None,
            "fit_feat_hit": None,
            "feat_coverage": 0.0,
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
    # feature-metadata ground truth (구조화 채점): color_any/fit_any → 실제 축 값
    exp_color_fams = _expected_color_families(colors)
    exp_fit_vals = _expected_fit_values(fits)

    subcat_hits = 0
    subcat_hits_tagged = 0
    tagged_count = 0
    keyword_hits = 0
    color_hits = 0
    fit_hits = 0
    color_feat_hits = 0
    fit_feat_hits = 0
    feat_present = 0
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

        # 구조화 속성 채점 — 상품 feature_metadata 실측 vs 기대 축.
        fmeta = row.get("fmeta") or {}
        if fmeta:
            feat_present += 1
        pcol = str(fmeta.get("primary_color") or "").strip().upper()
        pfit = str(fmeta.get("fit") or "").strip().lower()
        if exp_color_fams and pcol in exp_color_fams:
            color_feat_hits += 1
        if exp_fit_vals and pfit in exp_fit_vals:
            fit_feat_hits += 1

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
        # 구조화 속성 채점 (PRIMARY for "우와 비슷하다")
        "color_feat_hit": round(color_feat_hits / n, 3) if exp_color_fams else None,
        "fit_feat_hit": round(fit_feat_hits / n, 3) if exp_fit_vals else None,
        "feat_coverage": round(feat_present / n, 3),
        # legacy name-substring 채점 (진단용 — 상품명에 색·핏 적힌 경우만 잡음)
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
            # 구조화 속성 (PRIMARY)
            "color_feat_hit_mean": _mean_optional([r["scores"]["color_feat_hit"] for r in rs]),
            "fit_feat_hit_mean": _mean_optional([r["scores"]["fit_feat_hit"] for r in rs]),
            "feat_coverage_mean": _mean_optional([r["scores"]["feat_coverage"] for r in rs]),
            # legacy name-substring (진단)
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
    parser.add_argument(
        "--attr-align",
        action="store_true",
        help="속성정렬 rerank(fit/color) 적용 후 top-K — pool 검색→재정렬→절단",
    )
    parser.add_argument("--pool", type=int, default=60, help="--attr-align 재정렬 풀 크기 (default 60)")
    parser.add_argument("--limit", type=int, default=None, help="처음 N개만 실행")
    parser.add_argument("--label", default=None, help="결과 파일 라벨 (예: 'raw_baseline' / 'prod_baseline')")
    parser.add_argument(
        "--rewrite",
        action="store_true",
        help="KO 케이스를 LLM 으로 concise EN 키워드로 리라이트 후 embed (프로덕션 ReAct 에이전트 미러링). "
        "off 상태면 raw 텍스트를 그대로 임베드 → 순수 retrieval 품질 측정.",
    )
    parser.add_argument("--output", default=None, help="결과 저장 경로 (기본: results/search_quality_<sha>_<ts>.json)")
    args = parser.parse_args()

    modal_url = os.environ.get("MODAL_EMBED_URL")
    modal_token = os.environ.get("MODAL_EMBED_TOKEN")
    dsn = os.environ.get("KIKOAI_DEVAPP_DSN") or os.environ.get("DB_DSN")
    litellm_url = os.environ.get("LITELLM_BASE_URL")
    litellm_key = os.environ.get("LITELLM_MASTER_KEY", "")

    if not modal_url or not modal_token:
        print("ERROR: MODAL_EMBED_URL / MODAL_EMBED_TOKEN 환경변수 필요")
        sys.exit(1)
    if not dsn:
        print("ERROR: KIKOAI_DEVAPP_DSN 또는 DB_DSN 환경변수 필요")
        sys.exit(1)
    if args.rewrite and not litellm_url:
        print("ERROR: --rewrite 사용 시 LITELLM_BASE_URL 환경변수 필요")
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

    logger.info(
        "로드 완료: %d 케이스 · top_k=%d · rewrite=%s · sha=%s · branch=%s",
        len(cases),
        args.top_k,
        args.rewrite,
        sha,
        branch,
    )

    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient() as http_client:
        conn = psycopg.connect(dsn, application_name="search_quality_eval")
        try:
            for i, case in enumerate(cases, 1):
                try:
                    raw_input = case["input"]
                    if args.rewrite:
                        embed_input = await _rewrite_query(
                            http_client, litellm_url, litellm_key, raw_input, case.get("lang")
                        )
                    else:
                        embed_input = raw_input
                    query_vec = await _embed_text(http_client, modal_url, modal_token, embed_input)
                    pool_k = args.pool if args.attr_align else args.top_k
                    rows = _search_products_v6(conn, query_embedding=query_vec, top_k=pool_k)
                    _attach_feature_metadata(conn, rows)
                    if args.attr_align:
                        rows = _attr_align_rerank(rows, case.get("expected", {}))[: args.top_k]
                    scores = _score_case(rows, case.get("expected", {}))
                    results.append(
                        {
                            "id": case["id"],
                            "type": case["type"],
                            "input": raw_input,
                            "embed_input": embed_input,  # after rewrite (or same as input if off)
                            "lang": case.get("lang"),
                            "scores": scores,
                        }
                    )
                    rewrite_note = f' → "{embed_input}"' if args.rewrite and embed_input != raw_input else ""
                    _cf = scores["color_feat_hit"]
                    _ff = scores["fit_feat_hit"]
                    logger.info(
                        "[%d/%d] %s [%s] Q=%.2f kw=%.2f cf=%s ff=%s cov=%.2f n=%d%s",
                        i,
                        len(cases),
                        case["id"],
                        case["type"],
                        scores["quality_score"],
                        scores["keyword_hit"],
                        f"{_cf:.2f}" if _cf is not None else "—",
                        f"{_ff:.2f}" if _ff is not None else "—",
                        scores["feat_coverage"],
                        scores["result_count"],
                        rewrite_note,
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
                    "rewrite": args.rewrite,
                    "rewrite_model": _REWRITE_MODEL if args.rewrite else None,
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
