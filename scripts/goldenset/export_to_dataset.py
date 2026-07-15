"""골든셋 S 판정 값 → tests/eval/search_quality_dataset.json 이관.

사람이 눈으로 S(성공) 판정한 문형×값을 자동 채점 하네스의 회귀 케이스로 변환한다.
검색 스택 변경(임베딩/리라이트/필터/카탈로그) 시 search_quality_eval.py 재실행으로
골든셋 통과율 하락을 숫자로 감지하는 것이 목적.

변환 규칙 (판별력 있는 슬롯을 keywords_any 로):
  p1 컬러   → color_any=[컬러], keywords_any=[아이템]
  p2 소재   → keywords_any=[소재 토큰]  (소재가 빠지면 점수 하락하도록)
  p3 핏     → fit_any=[핏 토큰], keywords_any=[아이템]
  p4/p5/p7  → keywords_any=[아이템]  (무드/씬/TPO 자체는 자동 채점 불가 —
              카테고리 이탈만 감시. 원 판정 근거는 note 에 보존)
  p6 패턴   → keywords_any=[패턴 토큰]

idempotent: 기존 `gs_` prefix 케이스를 제거 후 재생성. 원본은 .bak 백업.

사용: uv run python scripts/goldenset/export_to_dataset.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from matrix import get_patterns  # noqa: E402

DATASET = ROOT / "tests" / "eval" / "search_quality_dataset.json"
JUDGMENTS = {"women": HERE / "out" / "judgments.json", "men": HERE / "out" / "judgments_men.json"}

# 아이템 접미사 → (keywords, subcategory_any). 긴 접미사 우선 매칭.
ITEMS: list[tuple[str, list[str], list[str]]] = [
    ("trench coat", ["trench"], ["trench-coat"]),
    ("chore coat", ["chore"], ["chore-jacket"]),
    ("puffer jacket", ["puffer", "down"], ["down-jacket"]),
    ("varsity jacket", ["varsity"], ["bomber"]),
    ("harrington jacket", ["harrington", "jacket"], ["bomber", "blazer"]),
    ("denim jacket", ["denim jacket", "trucker"], ["denim-jacket"]),
    ("leather jacket", ["leather"], ["leather-jacket"]),
    ("work jacket", ["work jacket", "chore"], ["chore-jacket", "field-jacket"]),
    ("souvenir jacket", ["souvenir", "sukajan"], ["bomber"]),
    ("windbreaker", ["windbreaker"], ["windbreaker"]),
    ("overshirt", ["overshirt", "shirt"], ["overshirt"]),
    ("camp shirt", ["camp", "shirt"], ["shirt"]),
    ("oxford shirt", ["oxford", "shirt"], ["shirt"]),
    ("flannel shirt", ["flannel"], ["shirt", "overshirt"]),
    ("sweatshirt", ["sweatshirt"], ["sweatshirt"]),
    ("t-shirt", ["t-shirt", "tee"], ["t-shirt"]),
    ("tee", ["tee", "t-shirt"], ["t-shirt"]),
    ("turtleneck", ["turtleneck", "high neck"], ["turtleneck"]),
    ("knit sweater", ["knit", "sweater"], ["sweater", "knit-top"]),
    ("sweater", ["sweater", "knit"], ["sweater", "knit-top"]),
    ("cardigan", ["cardigan"], ["cardigan"]),
    ("knit vest", ["vest"], ["vest"]),
    ("vest", ["vest"], ["vest"]),
    ("knit polo shirt", ["polo"], ["polo"]),
    ("polo shirt", ["polo"], ["polo"]),
    ("hoodie", ["hoodie", "hood"], ["hoodie"]),
    ("zip-up jacket", ["zip"], ["fleece", "hoodie"]),
    ("zip-up hoodie", ["zip", "hood"], ["hoodie"]),
    ("blouse", ["blouse"], ["blouse"]),
    ("knit", ["knit"], ["knit-top", "sweater"]),
    ("blazer", ["blazer"], ["blazer"]),
    ("parka", ["parka"], ["parka"]),
    ("coat", ["coat"], ["overcoat", "coat"]),
    ("jacket", ["jacket"], ["jacket"]),
    ("cargo pants", ["cargo"], ["cargo-pants"]),
    ("jeans", ["jeans", "denim"], ["jeans"]),
    ("chino shorts", ["shorts", "chino"], ["shorts", "chinos"]),
    ("shorts", ["shorts"], ["shorts"]),
    ("trousers", ["trousers", "slacks", "pants"], ["trousers"]),
    ("slacks", ["slacks", "trousers", "pants"], ["trousers"]),
    ("joggers", ["jogger", "sweatpants"], ["joggers", "sweatpants"]),
    ("leggings", ["leggings"], ["leggings"]),
    ("pants", ["pants", "trousers"], ["pants", "trousers"]),
    ("skirt", ["skirt"], ["skirt"]),
    ("dress", ["dress"], ["dress"]),
    ("sneakers", ["sneaker"], ["sneakers"]),
    ("boots", ["boots", "boot"], ["boots"]),
    ("loafers", ["loafer"], ["loafers"]),
    ("tote bag", ["tote"], ["tote"]),
    ("messenger bag", ["messenger"], ["messenger"]),
    ("bag", ["bag"], ["bag"]),
    ("loungewear set", ["lounge", "set"], []),
    ("top", ["top"], ["top"]),
]

COLOR_ALIASES = {
    "grey": ["grey", "gray"],
    "light blue": ["light blue", "blue"],
    "dark green": ["dark green", "green"],
    "deep purple": ["purple"],
    "cobalt blue": ["cobalt", "blue"],
}

# p6 패턴 토큰에서 제외할 조사어
_P6_STOPWORDS = {"print", "printed", "knit"}


def _split(en: str) -> tuple[str, list[str], list[str]]:
    """en 쿼리 → (modifier, item_keywords, subcategory_any). 긴 접미사 우선."""
    for suffix, kw, sub in ITEMS:
        if en.endswith(suffix):
            return en[: -len(suffix)].strip(), kw, sub
    return "", [en], []


def _build_case(gender: str, pattern: dict, value: dict) -> dict:
    en, ko = value["en"], value["ko"]
    modifier, item_kw, subcats = _split(en)
    pid = pattern["id"]
    expected: dict = {}
    if subcats:
        expected["subcategory_any"] = subcats

    if pid == "p1_color_category":
        expected["color_any"] = COLOR_ALIASES.get(modifier, [modifier])
        expected["keywords_any"] = item_kw
    elif pid == "p2_material_category":
        expected["keywords_any"] = [modifier] if modifier else item_kw
    elif pid == "p3_fit_category":
        expected["fit_any"] = [modifier] if modifier else []
        expected["keywords_any"] = item_kw
    elif pid == "p6_pattern_detail":
        tokens = [t for t in modifier.split() if t not in _P6_STOPWORDS]
        expected["keywords_any"] = [" ".join(tokens)] if tokens else item_kw
    else:  # p4 무드 / p5 에스테틱 / p7 상황 — 아이템 이탈만 감시
        expected["keywords_any"] = item_kw

    g = "w" if gender == "women" else "m"
    slug = en.replace(" ", "_").replace("'", "")
    prefix = "women's" if gender == "women" else "men's"
    return {
        "id": f"gs_{g}_{pid}_{slug}",
        "type": f"goldenset_{pid}",
        "input": f"{prefix} {en}",
        "lang": "en",
        "gender": gender,
        "note": (
            f"골든셋 human-judged S ({'260713' if gender == 'women' else '260714'}, ko='{ko}', "
            f"family_gate={value.get('category')}). 무드/씬/TPO의 질적 판정은 자동 채점 불가 — "
            "keyword/color/fit 이탈만 회귀 감시."
        ),
        "expected": expected,
    }


def main() -> None:
    ds = json.loads(DATASET.read_text())
    shutil.copy2(DATASET, DATASET.with_suffix(".json.bak"))

    before = len(ds["cases"])
    ds["cases"] = [c for c in ds["cases"] if not c["id"].startswith("gs_")]
    removed = before - len(ds["cases"])

    added = 0
    for gender, jpath in JUDGMENTS.items():
        judgments = json.loads(jpath.read_text())
        patterns = {p["id"]: p for p in get_patterns(gender)}
        for qid, grade in judgments.items():
            if grade != "S":
                continue
            pid, slug = qid.split("::")
            value = next(v for v in patterns[pid]["values"] if v["en"].replace(" ", "_") == slug)
            ds["cases"].append(_build_case(gender, patterns[pid], value))
            added += 1

    ds["version"] = "1.2"
    ds.setdefault("notes", []).append(
        "gs_* cases: 골든셋 human-judged S 값 이관 (scripts/goldenset/, 여성 260713 / 남성 260714). "
        "재생성: uv run python scripts/goldenset/export_to_dataset.py (idempotent)"
    )
    DATASET.write_text(json.dumps(ds, ensure_ascii=False, indent=4) + "\n")
    print(f"기존 gs_ 케이스 {removed}개 제거, {added}개 추가 → 총 {len(ds['cases'])}개 케이스")
    print(f"백업: {DATASET.with_suffix('.json.bak')}")


if __name__ == "__main__":
    main()
