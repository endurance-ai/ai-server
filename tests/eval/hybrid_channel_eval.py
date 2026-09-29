"""하이브리드 검색 채널별 기여 측정 (2026-09-29).

search_products_hybrid_v1 은 세 채널로 후보를 모은다.
  1. 이미지 임베딩 kNN (product_embeddings)
  2. 텍스트 임베딩 kNN (product_features.text_embedding), w_text 로 블렌드
  3. 상품명 trigram 매칭 (p_name_query 가 있을 때만, distance 에서 w_name 차감)

채널마다 올리려는 지표가 달라서 두 모드로 나눠 잰다.

  attr : 색 · 소재 · 패턴 속성 precision@15.
         골든셋 여성 p1(색) · p2(소재) · p6(패턴/디테일) 60쿼리를 실제 텍스트 검색
         경로(run_text_only_search: embed_text → RPC → 2tower → diversify, 익명
         유저라 개인화 재정렬 없음)로 돌리고, SEARCH_HYBRID_ENABLED 를 끈 상태
         (v6 이미지 단독)와 켠 상태(이미지+텍스트 블렌드)를 비교한다.
         채점은 규칙 기반: 결과 상품의 feature_metadata(VLM 추출 속성)가 아래
         _ATTR_TRUTH 의 기대값과 맞으면 hit. 상품명 채널은 name_query 가 없으면
         꺼지므로 이 모드에는 기여하지 않는다.

  name : 특정 상품(모델명 · 모델 코드) 찾기 hit@15.
         카탈로그에서 브랜드 안에서 드문 이름 토큰을 뽑아 검색어를 만들고
         (seed 고정), 같은 text_query · 브랜드로 name_query 를 넘길 때와 안 넘길
         때를 비교한다. 하이브리드 ON 상태에서만 의미가 있다.

8/3 PR #175 때의 파이프라인 재측정 스크립트는 저장소에 커밋되지 않아 남아 있지
않다. 이 스크립트는 같은 설계(같은 60쿼리, 같은 경로, 플래그 OFF/ON)를 새로
구현한 것이라 채점 규칙까지 8/3 과 같다는 보장은 없다.

실행 (ai-server 컨테이너 안, 배포된 코드와 env 그대로):
    docker cp tests/eval/hybrid_channel_eval.py ai-server:/tmp/
    docker cp scripts/goldenset/matrix.py ai-server:/tmp/
    docker exec -w /app ai-server python /tmp/hybrid_channel_eval.py attr --out /tmp/attr.json
    docker exec -w /app ai-server python /tmp/hybrid_channel_eval.py name --out /tmp/name.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
# 저장소에서 실행하면 scripts/goldenset, 컨테이너 /tmp 에서 실행하면 같은 폴더의 matrix.py 를 쓴다.
_REPO = _HERE.parents[1] if len(_HERE.parents) > 1 else _HERE
for p in (_HERE, _REPO / "scripts" / "goldenset", Path("/app")):
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

TOP_K = 15
CONCURRENCY = 3
SEED = 0.29

# ─────────────────────────────── attr 모드 채점표
# color   : primary_color 가 집합 안에 있으면 hit. VLM 색 계열(16개)에 없는 색은
#           가장 가까운 계열 1~2개로 매핑. 계열이 없는 색(silver)은 채점 제외.
# material: feature_metadata.material 과 교집합이 있으면 hit.
# pattern : feature_metadata.pattern 이 집합 안에 있으면 hit.
# kw      : feature_metadata.details 문구 중 하나가 정규식과 맞으면 hit
#           (소재 · 패턴 어휘에 없는 디테일용: 케이블, 아가일, 퀼팅 등).
_ATTR_TRUTH: dict[str, dict[str, Any]] = {
    # p1 색
    "burgundy cardigan": {"color": {"RED", "PURPLE"}},
    "khaki shirt": {"color": {"KHAKI", "GREEN"}},
    "navy blazer": {"color": {"NAVY"}},
    "beige trench coat": {"color": {"BEIGE", "CREAM"}},
    "black slip dress": {"color": {"BLACK"}},
    "white sneakers": {"color": {"WHITE", "CREAM"}},
    "charcoal slacks": {"color": {"GREY", "BLACK"}},
    "brown loafers": {"color": {"BROWN"}},
    "olive cargo pants": {"color": {"GREEN", "KHAKI"}},
    "ivory blouse": {"color": {"CREAM", "WHITE"}},
    "red knit vest": {"color": {"RED"}},
    "light blue denim jacket": {"color": {"BLUE"}},
    "lavender hoodie": {"color": {"PURPLE"}},
    "mustard sweater": {"color": {"YELLOW"}},
    "grey trousers": {"color": {"GREY"}},
    "pink midi skirt": {"color": {"PINK"}},
    "silver mini bag": None,
    "dark green coat": {"color": {"GREEN"}},
    "orange t-shirt": {"color": {"ORANGE"}},
    "deep purple dress": {"color": {"PURPLE"}},
    # p2 소재
    "leather jacket": {"material": {"leather"}},
    "wool coat": {"material": {"wool"}},
    "corduroy pants": {"material": {"corduroy"}},
    "denim skirt": {"material": {"denim"}},
    "cashmere knit sweater": {"material": {"cashmere"}},
    "linen shirt": {"material": {"linen"}},
    "suede boots": {"material": {"suede"}},
    "tweed jacket": {"material": {"tweed"}},
    "velvet dress": {"material": {"velvet"}},
    "satin skirt": {"material": {"satin", "silk"}},
    "mohair cardigan": {"kw": r"mohair"},
    "cotton zip-up hoodie": {"material": {"cotton"}},
    "silk blouse": {"material": {"silk", "satin"}},
    "nylon windbreaker": {"material": {"nylon", "ripstop"}},
    "shearling jacket": {"kw": r"shearling|sherpa"},
    "ribbed knit top": {"kw": r"\brib"},
    "mesh top": {"material": {"mesh"}},
    "canvas tote bag": {"material": {"canvas"}},
    "fleece zip-up jacket": {"material": {"fleece"}},
    "knit polo shirt": {"material": {"knit"}},
    # p6 패턴 · 디테일
    "striped shirt": {"pattern": {"striped"}},
    "checked jacket": {"pattern": {"checked"}},
    "cable knit sweater": {"kw": r"cable"},
    "argyle knit vest": {"kw": r"argyle"},
    "floral print dress": {"pattern": {"floral"}},
    "polka dot blouse": {"pattern": {"dot"}},
    "houndstooth coat": {"kw": r"houndstooth"},
    "leopard print skirt": {"pattern": {"animal"}},
    "plaid tartan skirt": {"pattern": {"checked"}},
    "graphic print t-shirt": {"pattern": {"graphic"}},
    "embroidered shirt": {"kw": r"embroider"},
    "quilted jacket": {"kw": r"quilt"},
    "fringe bag": {"kw": r"fring"},
    "ruffle blouse": {"kw": r"ruffle|frill"},
    "lace top": {"kw": r"\blace\b(?!-up| up)"},
    "pinstripe trousers": {"pattern": {"striped"}},
    "tie-dye t-shirt": {"kw": r"tie[- ]?dye"},
    "colorblock knit sweater": {"pattern": {"colorblock"}},
    "cargo pocket pants": {"kw": r"cargo"},
    "cutout dress": {"kw": r"cut[- ]?out"},
}
_ATTR_PATTERNS = {"p1_color_category": "color", "p2_material_category": "material", "p6_pattern_detail": "pattern"}


def _materials(fm: dict[str, Any]) -> set[str]:
    raw = fm.get("material")
    if isinstance(raw, list):
        return {str(m).strip().lower() for m in raw}
    return {str(raw).strip().lower()} if raw else set()


def _is_hit(fm: dict[str, Any] | None, truth: dict[str, Any]) -> bool:
    if not fm:
        return False
    if "color" in truth and str(fm.get("primary_color") or "").upper() in truth["color"]:
        return True
    if "material" in truth and _materials(fm) & truth["material"]:
        return True
    if "pattern" in truth and str(fm.get("pattern") or "").lower() in truth["pattern"]:
        return True
    if "kw" in truth:
        details = fm.get("details") or []
        if isinstance(details, str):
            details = [details]
        if any(re.search(truth["kw"], str(d).lower()) for d in details):
            return True
    return False


def _cand_id(c: Any) -> str | None:
    d = c.model_dump() if hasattr(c, "model_dump") else (c if isinstance(c, dict) else {})
    v = d.get("id") or d.get("product_id")
    return str(v) if v is not None else None


def _cand_name(c: Any) -> str:
    d = c.model_dump() if hasattr(c, "model_dump") else (c if isinstance(c, dict) else {})
    return str(d.get("name") or "")


async def _search(**kwargs: Any) -> list[Any]:
    """run_text_only_search 를 일시적 네트워크 오류(httpx.ReadError 등)에 대해 3회까지 재시도."""
    from app.agents.tools.search_products import run_text_only_search

    for attempt in range(3):
        try:
            return await run_text_only_search(**kwargs)
        except Exception:  # noqa: BLE001 — 평가 스크립트: 마지막 시도에서만 올린다
            if attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    return []


def _fetch_features(conn: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "select product_id::text, feature_metadata from product_features where product_id::text = any(%s)",
            (ids,),
        )
        return {pid: (fm or {}) for pid, fm in cur.fetchall()}


async def _run_attr(conn: Any) -> dict[str, Any]:
    from matrix import get_patterns

    from app.core.config import settings

    queries = []
    for p in get_patterns("women"):
        if p["id"] in _ATTR_PATTERNS:
            for v in p["values"]:
                queries.append((p["id"], v))

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(pid: str, v: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            cands = await _search(
                text_query=f"women's {v['en']}", category=v.get("category"), gender="women", top_k=TOP_K
            )
        return {"pattern": pid, "en": v["en"], "ids": [i for i in (_cand_id(c) for c in cands) if i]}

    out: dict[str, Any] = {}
    for label, flag in (("image_only", False), ("hybrid", True)):
        settings.SEARCH_HYBRID_ENABLED = flag
        rows = await asyncio.gather(*[one(pid, v) for pid, v in queries])
        feats = _fetch_features(conn, sorted({i for r in rows for i in r["ids"]}))
        for r in rows:
            truth = _ATTR_TRUTH.get(r["en"])
            if truth is None or not r["ids"]:
                r["precision"] = None if truth is None else 0.0
                continue
            hits = [_is_hit(feats.get(i), truth) for i in r["ids"]]
            r["n"] = len(hits)
            r["precision"] = round(sum(hits) / len(hits), 4)
        out[label] = rows
        print(f"[attr] {label} 완료 ({len(rows)} 쿼리)", flush=True)

    summary: dict[str, Any] = {}
    for label, rows in out.items():
        s: dict[str, Any] = {}
        for pid, axis in _ATTR_PATTERNS.items():
            vals = [r["precision"] for r in rows if r["pattern"] == pid and r["precision"] is not None]
            s[axis] = {"mean": round(sum(vals) / len(vals), 4), "n": len(vals)}
        s["mean_of_axes"] = round(sum(s[a]["mean"] for a in _ATTR_PATTERNS.values()) / 3, 4)
        summary[label] = s
    return {"summary": summary, "rows": out}


# ─────────────────────────────── name 모드
_NAME_SAMPLE_SQL = """
with w as (
  select p.id::text id, p.brand, p.name, p.category, p.subcategory, lower(t) tok
  from products p, regexp_split_to_table(p.name, '[\\s,/()\\[\\]|_+.:;''"-]+') t
  where p.in_stock and length(t) >= 4 and t ~ '^[A-Za-z0-9]+$'
),
big as (select brand from products where in_stock group by 1 having count(*) >= 200),
cat as (select tok, count(distinct id) n from w group by 1),
bt as (select brand, tok, count(distinct id) n from w group by 1, 2),
cand as (
  select w.*, case when w.tok ~ '[0-9]' then 'code' else 'word' end kind
  from w join bt using (brand, tok) join cat using (tok) join big using (brand)
  where bt.n <= 3 and cat.n <= 20 and w.tok !~ '^[0-9]+$'
    -- 시즌(ss23 · fw24) · 중량(13oz) 표기는 모델 식별자가 아니라 제외
    and w.tok !~ '^(ss|fw|aw|pf)[0-9]{2,4}$' and w.tok !~ '^[0-9]+oz$'
),
one_per_brand as (
  select distinct on (kind, brand) * from cand order by kind, brand, md5(id || tok || %(seed)s)
)
select kind, id, brand, name, category, subcategory, tok
from (select *, row_number() over (partition by kind order by md5(brand || %(seed)s)) rn from one_per_brand) x
where rn <= %(per_kind)s
order by kind, brand
"""


_NAME_MODES = ("none", "text", "name")


async def _run_name(conn: Any, per_kind: int) -> dict[str, Any]:
    from app.core.config import settings

    settings.SEARCH_HYBRID_ENABLED = True
    with conn.cursor() as cur:
        # random() 은 병렬 쿼리 워커마다 시드가 달라 재현되지 않아 md5 해시 순서로 뽑는다.
        cur.execute(_NAME_SAMPLE_SQL, {"seed": str(SEED), "per_kind": per_kind})
        samples = [
            dict(zip(("kind", "id", "brand", "name", "category", "subcategory", "tok"), r, strict=True))
            for r in cur.fetchall()
        ]

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(s: dict[str, Any], name_mode: str, use_brand: bool) -> dict[str, Any]:
        # name_mode: none = 토큰을 아예 안 넘김, text = text_query 에 토큰을 붙임(이름 채널 없이
        # 임베딩만으로 찾기), name = name_query 로 넘김(상품명 채널).
        base = s["subcategory"] or s["category"] or "fashion"
        async with sem:
            cands = await _search(
                text_query=f"{base} {s['tok']}" if name_mode == "text" else base,
                category=s["category"],
                brand_filter=[s["brand"]] if use_brand else None,
                name_query=s["tok"] if name_mode == "name" else None,
                top_k=TOP_K,
            )
        ids = [_cand_id(c) for c in cands]
        names = [_cand_name(c).lower() for c in cands]
        return {
            "exact": s["id"] in ids,
            "same_token": any(re.search(rf"(?<![a-z0-9]){re.escape(s['tok'])}(?![a-z0-9])", n) for n in names),
        }

    rows = []
    for s in samples:
        r = dict(s)
        for use_brand in (True, False):
            for name_mode in _NAME_MODES:
                key = f"{'brand' if use_brand else 'nobrand'}_{name_mode}"
                try:
                    r[key] = await one(s, name_mode, use_brand)
                except Exception as exc:  # noqa: BLE001 — 재시도 후에도 실패한 표본은 집계에서 뺀다
                    r["error"] = repr(exc)
        rows.append(r)
        print(f"[name] {s['kind']} {s['brand']} / {s['tok']} 완료", flush=True)

    summary: dict[str, Any] = {}
    for kind in ("code", "word", "all"):
        sub = [r for r in rows if (kind == "all" or r["kind"] == kind) and "error" not in r]
        k: dict[str, Any] = {"n": len(sub), "errors": sum(1 for r in rows if "error" in r)}
        for cond in (f"{b}_{m}" for b in ("brand", "nobrand") for m in _NAME_MODES):
            k[cond] = {
                "exact_hit": round(sum(r[cond]["exact"] for r in sub) / len(sub), 4) if sub else None,
                "same_token_hit": round(sum(r[cond]["same_token"] for r in sub) / len(sub), 4) if sub else None,
            }
        summary[kind] = k
    return {"summary": summary, "rows": rows}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["attr", "name"])
    ap.add_argument("--per-kind", type=int, default=50, help="name 모드: 코드/단어 토큰별 표본 수")
    ap.add_argument(
        "--attr-align",
        choices=["on", "off"],
        default="on",
        help="속성 재정렬(ATTR_ALIGN_ENABLED). off 는 8/3 측정 당시처럼 임베딩 순위만 본다",
    )
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    import psycopg

    from app.core.config import settings
    from app.providers import db_pool

    settings.ATTR_ALIGN_ENABLED = args.attr_align == "on"
    try:
        await db_pool.init_pool(settings.DB_DSN)
    except Exception as exc:  # noqa: BLE001 — 임베딩 캐시만 못 쓰고 계속
        print(f"db_pool init 실패, 캐시 없이 진행: {exc!r}", flush=True)

    t0 = time.time()
    with psycopg.connect(settings.DB_DSN) as conn:
        result = await (_run_attr(conn) if args.mode == "attr" else _run_name(conn, args.per_kind))

    image_tag = subprocess.run(["sh", "-c", "echo ${IMAGE_TAG:-}"], capture_output=True, text=True).stdout.strip()
    result["meta"] = {
        "mode": args.mode,
        "top_k": TOP_K,
        "attr_align": settings.ATTR_ALIGN_ENABLED,
        "w_text": settings.SEARCH_HYBRID_W_TEXT,
        "pool": settings.SEARCH_HYBRID_POOL,
        "seed": SEED,
        "image_tag": image_tag or None,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps(result["summary"], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
