"""골든셋 문형 평가 러너 — 자체 앱 텍스트 검색 경로 그대로 실행.

matrix.py 의 문형×값 매트릭스를 run_text_only_search (embed_text → search_products_v6
→ diversify) 로 실행하고, 사람이 직접 판정할 수 있는 HTML 리포트를 생성한다.

사용 (repo 루트에서):
    uv run python scripts/goldenset/run_goldenset.py                 # 전체 실행
    uv run python scripts/goldenset/run_goldenset.py p4_mood_category  # 특정 문형만
    uv run python scripts/goldenset/run_goldenset.py --gender none  # 성별 토큰 미부착

출력:
    scripts/goldenset/out/results.json   — 원시 결과 (재실행 시 리포트만 다시 생성 가능)
    scripts/goldenset/out/report.html    — 판정용 리포트 (브라우저에서 열기)

판정 버튼(성공/애매/실패)은 localStorage 에 저장되며, 리포트 상단 "판정 내보내기"
버튼으로 JSON 다운로드 가능.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from matrix import get_patterns  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.providers import db_pool  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "out"


def _out_paths(gender: str, precision: str) -> tuple[Path, Path]:
    suffix = f"_{gender}" if gender != "women" else ""
    # 2026-07-15 A/B: --precision off 런은 별도 파일로 (전후 비교용)
    if precision == "off":
        suffix += "_precoff"
    return OUT_DIR / f"results{suffix}.json", OUT_DIR / f"report{suffix}.html"


TOP_K = 10
CONCURRENCY = 3


def _cand_to_dict(cand: Any) -> dict[str, Any]:
    if hasattr(cand, "model_dump"):
        d = cand.model_dump()
    elif isinstance(cand, dict):
        d = dict(cand)
    else:
        d = {
            k: getattr(cand, k, None)
            for k in ("id", "brand", "name", "price", "image_url", "product_url", "platform", "score")
        }
    return {
        "id": d.get("id") or d.get("product_id"),
        "brand": d.get("brand"),
        "name": d.get("name") or d.get("title"),
        "price": d.get("price"),
        "image_url": d.get("image_url") or d.get("imageUrl"),
        "product_url": d.get("product_url") or d.get("productUrl"),
        "platform": d.get("platform"),
        "score": d.get("score"),
        # 2026-07-15 — p8_subcategory 자동 hit율용 (RPC row 의 subcategory)
        "subcategory": d.get("subcategory"),
    }


async def _run_one(sem: asyncio.Semaphore, pattern: dict, value: dict, gender: str, precision: str) -> dict[str, Any]:
    from app.agents.tools.search_products import run_multi_query_search, run_text_only_search

    en = value["en"]
    query = f"women's {en}" if gender == "women" else (f"men's {en}" if gender == "men" else en)
    qid = f"{pattern['id']}::{en.replace(' ', '_')}"
    g = gender if gender in ("men", "women") else None
    # Phase 4a — sub_queries 가 있고 precision on 이면 멀티 확장(실제 봇의 LLM
    # sub_queries 를 통제값으로 재현). off 는 단일 검색(현행 baseline).
    sub_queries = value.get("sub_queries") if precision == "on" else None
    async with sem:
        t0 = time.perf_counter()
        try:
            if sub_queries:
                cands = await run_multi_query_search(
                    queries=[en, *sub_queries],
                    gender=g,
                    top_k=TOP_K,
                )
            else:
                cands = await run_text_only_search(
                    text_query=query,
                    category=value.get("category"),
                    # 2026-07-16 — v6 p_gender 하드 필터: 실제 봇 경로와 동일하게
                    # 구조화 성별도 전달 (none 런은 필터 off).
                    gender=g,
                    top_k=TOP_K,
                )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            results = [_cand_to_dict(c) for c in cands]
            # 2026-07-15 — 자동 subcategory hit율 (라벨 있는 값만): 결과 row 의
            # subcategory 가 정답과 EXACT 일치하는 비율. NULL row 는 miss 취급
            # (라벨 미부여 상품 — 정밀도 관점에서 보수적으로 계산).
            target = value.get("target_subcategory")
            subcat_hit = None
            if target and results:
                subcat_hit = round(sum(1 for c in results if c.get("subcategory") == target) / len(results), 3)
            hit_str = f" subcat_hit={subcat_hit:.0%}" if subcat_hit is not None else ""
            print(f"  ✓ [{pattern['id']}] {en!r} → {len(results)}건 ({elapsed_ms}ms){hit_str}", flush=True)
            return {
                "qid": qid,
                "ko": value["ko"],
                "en": en,
                "query": query,
                "category": value.get("category"),
                "target_subcategory": target,
                "subcat_hit": subcat_hit,
                "elapsed_ms": elapsed_ms,
                "error": None,
                "results": results,
            }
        except Exception as exc:  # noqa: BLE001 — 평가 스크립트: 한 쿼리 실패가 전체를 막지 않게
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            print(f"  ✗ [{pattern['id']}] {en!r} → ERROR {exc!r}", flush=True)
            return {
                "qid": qid,
                "ko": value["ko"],
                "en": en,
                "query": query,
                "category": value.get("category"),
                "target_subcategory": value.get("target_subcategory"),
                "subcat_hit": None,
                "elapsed_ms": elapsed_ms,
                "error": repr(exc),
                "results": [],
            }


def _apply_precision(precision: str) -> None:
    """2026-07-15 전후 비교 A/B 스위치. off = 배포 이전 동작 재현 (p_subcategory/
    p_color_family 미사용 — kill-switch 플래그로 동일 코드에서 재현). on = 현행
    프로덕션 동작 (canonical vocab 매칭 + family 정렬 + 완화 재시도).

    참고: 구 6-param/7-param 오버로드 공존으로 인한 PGRST203 회피 패치는
    2026-07-15 제거 — DB 에 7-param 단일 함수만 존재 (PR #145/#146)."""
    on = precision == "on"
    settings.SEARCH_SUBCATEGORY_FILTER_ENABLED = on
    settings.SEARCH_COLOR_FILTER_ENABLED = on
    # 2026-07-16 — p_gender 하드 필터도 같은 스위치로 (off = 배포 전 재현).
    settings.SEARCH_GENDER_FILTER_ENABLED = on
    print(f"precision filters: {'ON (현행)' if on else 'OFF (배포 전 재현)'}", flush=True)


async def run_matrix(pattern_ids: list[str] | None, gender: str, precision: str) -> dict[str, Any]:
    _apply_precision(precision)
    try:
        await db_pool.init_pool(settings.DB_DSN)
    except Exception as exc:  # noqa: BLE001 — 캐시는 fail-open, Modal 본 경로로 계속
        print(f"⚠ db_pool init 실패 (embedding cache 미사용으로 계속): {exc!r}", flush=True)

    sem = asyncio.Semaphore(CONCURRENCY)
    patterns = [p for p in get_patterns(gender) if not pattern_ids or p["id"] in pattern_ids]
    out: dict[str, Any] = {
        "gender": gender,
        "top_k": TOP_K,
        "precision": precision,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "patterns": [],
    }
    for p in patterns:
        print(f"\n=== {p['id']} — {p['name_ko']} ({len(p['values'])}개 값) ===", flush=True)
        rows = await asyncio.gather(*[_run_one(sem, p, v, gender, precision) for v in p["values"]])
        entry: dict[str, Any] = {"id": p["id"], "name_ko": p["name_ko"], "tier": p["tier"], "queries": list(rows)}
        # 패턴 단위 자동 지표 — target_subcategory 라벨이 있는 값들의 평균 hit율
        hits = [q["subcat_hit"] for q in rows if q.get("subcat_hit") is not None]
        if hits:
            entry["avg_subcat_hit"] = round(sum(hits) / len(hits), 3)
            print(f"  ── avg subcat_hit = {entry['avg_subcat_hit']:.1%} ({len(hits)}개 쿼리)", flush=True)
        out["patterns"].append(entry)
    return out


# ─────────────────────────────────────────── HTML 리포트

_CSS = """
body{font-family:'Apple SD Gothic Neo','Pretendard',sans-serif;margin:0;background:#fafafa;color:#1a1a1a}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #e5e5e5;padding:12px 24px;z-index:10;
  display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:16px;margin:0}
#summary{font-size:13px;color:#555}
button.export{margin-left:auto;padding:6px 14px;border:1px solid #1a1a1a;background:#1a1a1a;color:#fff;
  border-radius:6px;cursor:pointer;font-size:13px}
section.pattern{margin:24px;border:1px solid #e5e5e5;border-radius:12px;background:#fff;overflow:hidden}
section.pattern>h2{font-size:15px;margin:0;padding:14px 20px;background:#f4f4f4;border-bottom:1px solid #e5e5e5;
  display:flex;gap:10px;align-items:baseline}
.tier{font-size:11px;padding:2px 8px;border-radius:10px;background:#e8e8e8;color:#555}
.pstat{font-size:12px;color:#777;margin-left:auto}
.qrow{padding:14px 20px;border-bottom:1px solid #f0f0f0}
.qhead{display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap}
.qko{font-weight:700;font-size:14px}
.qen{font-size:12px;color:#888;font-family:monospace}
.qmeta{font-size:11px;color:#aaa}
.judge{display:flex;gap:6px;margin-left:auto}
.judge button{padding:4px 12px;border-radius:14px;border:1px solid #ccc;background:#fff;cursor:pointer;font-size:12px}
.judge button.on-S{background:#16a34a;border-color:#16a34a;color:#fff}
.judge button.on-M{background:#f59e0b;border-color:#f59e0b;color:#fff}
.judge button.on-F{background:#dc2626;border-color:#dc2626;color:#fff}
.cards{display:flex;gap:10px;overflow-x:auto;padding-bottom:6px}
.card{flex:0 0 130px;font-size:11px}
.card img{width:130px;height:165px;object-fit:cover;border-radius:8px;background:#eee;display:block}
.card .b{font-weight:600;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .n{color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .p{color:#999}
.err{color:#dc2626;font-size:12px}
"""

_JS = """
const KEY='goldenset_judgments_v1_'+(document.documentElement.dataset.gender||'women');
const load=()=>JSON.parse(localStorage.getItem(KEY)||'{}');
const save=(j)=>localStorage.setItem(KEY,JSON.stringify(j));
function judge(qid,v){const j=load();j[qid]=(j[qid]===v)?null:v;if(!j[qid])delete j[qid];save(j);render();}
function render(){
  const j=load();
  document.querySelectorAll('.qrow').forEach(r=>{
    const qid=r.dataset.qid,v=j[qid];
    r.querySelectorAll('.judge button').forEach(b=>{
      b.className=(v===b.dataset.v)?('on-'+v):'';});
  });
  document.querySelectorAll('section.pattern').forEach(s=>{
    let c={S:0,M:0,F:0},total=0;
    s.querySelectorAll('.qrow').forEach(r=>{total++;const v=j[r.dataset.qid];if(v)c[v]++;});
    s.querySelector('.pstat').textContent=`성공 ${c.S} · 애매 ${c.M} · 실패 ${c.F} / ${total}`;
  });
  const all=Object.keys(j).length;
  document.getElementById('summary').textContent=`판정 완료 ${all}건 (브라우저에 자동 저장됨)`;
}
function exportJudgments(){
  const blob=new Blob([JSON.stringify(load(),null,2)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='goldenset_judgments.json';a.click();
}
window.addEventListener('DOMContentLoaded',render);
"""


def build_report(data: dict[str, Any]) -> str:
    e = html.escape
    # 판정 localStorage 키 분리: precision off 런은 별도 키 (on 런은 기존 키 유지)
    key_ns = data["gender"] + ("_precoff" if data.get("precision") == "off" else "")
    parts = [
        f"<!doctype html><html lang='ko' data-gender='{html.escape(key_ns)}'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Kiko 골든셋 문형 판정 리포트</title>",
        f"<style>{_CSS}</style><script>{_JS}</script></head><body>",
        "<header><h1>골든셋 문형 판정</h1>",
        # precision A/B 라벨 — 두 리포트를 나란히 볼 때 전/후 구분용
        (
            "<span style='font-weight:800;font-size:13px;color:#fff;background:#dc2626;"
            "padding:3px 10px;border-radius:12px'>🔴 정밀 필터 OFF (배포 전 재현)</span>"
            if data.get("precision") == "off"
            else "<span style='font-weight:800;font-size:13px;color:#fff;background:#16a34a;"
            "padding:3px 10px;border-radius:12px'>🟢 정밀 필터 ON</span>"
        ),
        f"<span id='summary'></span><span class='qmeta'>gender={e(data['gender'])} · "
        f"top_k={data['top_k']} · {e(data['generated_at'])}</span>",
        "<button class='export' onclick='exportJudgments()'>판정 내보내기 (JSON)</button></header>",
    ]
    for p in data["patterns"]:
        parts.append(
            f"<section class='pattern'><h2>{e(p['name_ko'])} <span class='tier'>{e(p['tier'])}</span>"
            f"<span class='pstat'></span></h2>"
        )
        for q in p["queries"]:
            parts.append(
                f"<div class='qrow' data-qid='{e(q['qid'])}'><div class='qhead'>"
                f"<span class='qko'>{e(q['ko'])}</span><span class='qen'>{e(q['query'])}</span>"
                f"<span class='qmeta'>gate={e(str(q['category']))} · {q['elapsed_ms']}ms · {len(q['results'])}건</span>"
                f"<div class='judge'>"
                f"<button data-v='S' onclick=\"judge('{e(q['qid'])}','S')\">성공</button>"
                f"<button data-v='M' onclick=\"judge('{e(q['qid'])}','M')\">애매</button>"
                f"<button data-v='F' onclick=\"judge('{e(q['qid'])}','F')\">실패</button>"
                f"</div></div>"
            )
            if q["error"]:
                parts.append(f"<div class='err'>ERROR: {e(q['error'])}</div>")
            parts.append("<div class='cards'>")
            for c in q["results"]:
                img = e(c.get("image_url") or "")
                price = f"{c['price']:,}원" if isinstance(c.get("price"), int) else ""
                score = f"{c['score']:.3f}" if isinstance(c.get("score"), float) else ""
                link = e(c.get("product_url") or "#")
                parts.append(
                    f"<div class='card'><a href='{link}' target='_blank'>"
                    f"<img src='{img}' loading='lazy' alt=''></a>"
                    f"<div class='b'>{e(str(c.get('brand') or ''))}</div>"
                    f"<div class='n'>{e(str(c.get('name') or ''))}</div>"
                    f"<div class='p'>{price} · {score}</div></div>"
                )
            parts.append("</div></div>")
        parts.append("</section>")
    parts.append("</body></html>")
    return "".join(parts)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern_ids", nargs="*", help="실행할 문형 id (생략 시 전체)")
    ap.add_argument("--gender", choices=["women", "men", "none"], default="women")
    ap.add_argument(
        "--precision",
        choices=["on", "off"],
        default="on",
        help="정밀 필터 A/B: on=현행(기본), off=배포 전 재현 (별도 _precoff 파일로 저장)",
    )
    ap.add_argument("--report-only", action="store_true", help="기존 results.json으로 리포트만 재생성")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path, report_path = _out_paths(args.gender, args.precision)
    if args.report_only:
        data = json.loads(results_path.read_text())
    else:
        data = await run_matrix(args.pattern_ids or None, args.gender, args.precision)
        results_path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        print(f"\n결과 저장: {results_path}")
    report_path.write_text(build_report(data))
    print(f"리포트 생성: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
