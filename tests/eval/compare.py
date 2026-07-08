"""Diff two search-quality eval runs.

Usage:
    uv run python tests/eval/compare.py <before.json> <after.json>
    uv run python tests/eval/compare.py <before.json> <after.json> --metric keyword_hit
    uv run python tests/eval/compare.py <before.json> <after.json> --show-samples

Reports:
    - overall metric delta (before → after with sign)
    - by-type breakdown
    - per-case table sorted by |delta| descending (which cases moved most)
    - improved / regressed / unchanged counts
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_METRICS = [
    "quality_score",
    "subcat_hit",
    "keyword_hit",
    "color_hit",
    "fit_hit",
    "brand_hit",
    "brand_diversity",
    "distance_p50",
]
_LOWER_IS_BETTER = {"distance_p50"}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt_delta(a: float | None, b: float | None, metric: str) -> str:
    if a is None or b is None:
        return "  ─  "
    diff = b - a
    lower_better = metric in _LOWER_IS_BETTER
    # improvement direction depends on metric semantics
    good = (diff < 0) if lower_better else (diff > 0)
    sign = "+" if diff > 0 else ""
    marker = "🟢" if good and abs(diff) >= 0.01 else ("🔴" if not good and abs(diff) >= 0.01 else "  ")
    return f"{sign}{diff:+.3f} {marker}"


def _diff_summary(before: dict[str, Any], after: dict[str, Any]) -> None:
    b_meta = before.get("meta", {})
    a_meta = after.get("meta", {})
    print("=" * 78)
    print(f"BEFORE  sha={b_meta.get('sha')}  label={b_meta.get('label')}  ts={b_meta.get('timestamp')}")
    print(f"AFTER   sha={a_meta.get('sha')}  label={a_meta.get('label')}  ts={a_meta.get('timestamp')}")
    print("=" * 78)

    b_ov = before["summary"]["overall"]
    a_ov = after["summary"]["overall"]
    print(f"\n{'Overall metric':<24}{'Before':>12}{'After':>12}{'Δ':>16}")
    print("-" * 64)
    for m in _METRICS:
        key = m + "_mean"
        bv = b_ov.get(key)
        av = a_ov.get(key)
        bstr = f"{bv:.3f}" if bv is not None else "  ─  "
        astr = f"{av:.3f}" if av is not None else "  ─  "
        print(f"{m:<24}{bstr:>12}{astr:>12}   {_fmt_delta(bv, av, m)}")

    # By type
    print(f"\n{'By type · quality_score':<32}{'Before':>10}{'After':>10}{'Δ':>16}")
    print("-" * 68)
    b_types = before["summary"].get("by_type", {})
    a_types = after["summary"].get("by_type", {})
    for t in sorted(set(b_types) | set(a_types)):
        bv = (b_types.get(t) or {}).get("quality_score_mean")
        av = (a_types.get(t) or {}).get("quality_score_mean")
        n = (a_types.get(t) or b_types.get(t) or {}).get("n", "?")
        bstr = f"{bv:.3f}" if bv is not None else "  ─  "
        astr = f"{av:.3f}" if av is not None else "  ─  "
        print(f"  {t:<22} n={n:<4}       {bstr:>10}{astr:>10}   {_fmt_delta(bv, av, 'quality_score')}")


def _diff_cases(before: dict[str, Any], after: dict[str, Any], metric: str, show_samples: bool) -> None:
    b_by_id = {r["id"]: r for r in before.get("results", [])}
    a_by_id = {r["id"]: r for r in after.get("results", [])}

    all_ids = sorted(set(b_by_id) | set(a_by_id))
    rows: list[tuple[str, float | None, float | None, float]] = []  # (id, before, after, |delta|)
    improved = regressed = unchanged = missing = 0

    for cid in all_ids:
        b = b_by_id.get(cid, {})
        a = a_by_id.get(cid, {})
        bv = (b.get("scores") or {}).get(metric)
        av = (a.get("scores") or {}).get(metric)
        if bv is None or av is None:
            missing += 1
            rows.append((cid, bv, av, -1.0))
            continue
        diff = av - bv
        lower_better = metric in _LOWER_IS_BETTER
        if abs(diff) < 0.01:
            unchanged += 1
        elif (diff < 0) if lower_better else (diff > 0):
            improved += 1
        else:
            regressed += 1
        rows.append((cid, bv, av, abs(diff)))

    rows.sort(key=lambda r: r[3], reverse=True)

    print(f"\nPer-case Δ ({metric}) — sorted by |Δ| desc")
    print("-" * 78)
    print(f"{'case_id':<36}{'Before':>10}{'After':>10}{'Δ':>18}")
    for cid, bv, av, _ in rows:
        bstr = f"{bv:.3f}" if bv is not None else "  ─  "
        astr = f"{av:.3f}" if av is not None else "  ─  "
        print(f"{cid:<36}{bstr:>10}{astr:>10}   {_fmt_delta(bv, av, metric)}")

    print("-" * 78)
    print(f"  improved: {improved}   regressed: {regressed}   unchanged: {unchanged}   missing: {missing}")

    if show_samples:
        print("\nSample-level top-K comparisons for cases with largest regression:")
        print("-" * 78)
        for cid, bv, av, _ in rows:
            if bv is None or av is None:
                continue
            diff = av - bv
            lower_better = metric in _LOWER_IS_BETTER
            regressed_case = (diff > 0) if lower_better else (diff < 0)
            if not regressed_case or abs(diff) < 0.05:
                continue
            b_samples = (b_by_id[cid].get("scores") or {}).get("samples") or []
            a_samples = (a_by_id[cid].get("scores") or {}).get("samples") or []
            print(f"\n  ▼ {cid}   {bv:.3f} → {av:.3f}")
            print("     BEFORE top-K:")
            for s in b_samples[:5]:
                print(f"       - {s.get('brand', ''):<16} {(s.get('name') or '')[:60]:<62} d={s.get('distance')}")
            print("     AFTER top-K:")
            for s in a_samples[:5]:
                print(f"       - {s.get('brand', ''):<16} {(s.get('name') or '')[:60]:<62} d={s.get('distance')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("before", help="이전 결과 JSON")
    parser.add_argument("after", help="이후 결과 JSON")
    parser.add_argument(
        "--metric",
        default="quality_score",
        choices=_METRICS,
        help="per-case 비교 메트릭 (default quality_score)",
    )
    parser.add_argument(
        "--show-samples",
        action="store_true",
        help="회귀 케이스의 top-K 상품 리스트 노출",
    )
    args = parser.parse_args()

    before = _load(Path(args.before))
    after = _load(Path(args.after))
    _diff_summary(before, after)
    _diff_cases(before, after, args.metric, args.show_samples)


if __name__ == "__main__":
    main()
