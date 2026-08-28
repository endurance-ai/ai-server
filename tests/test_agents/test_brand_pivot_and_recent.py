"""P0-a brand-pivot style carry + P2 recent-search ring buffer (2026-08-24).

Real trace (닝닝 공항패션 → 핑크 → 스킴스 → 다시 닝닝): a brand pivot
("스킴스로 보여줘") dropped all prior style and searched a bare "skims top
women" → SKIMS catalog default is underwear → thongs/bras. And "다시 닝닝
스타일" lost the anchor because `_LAST` had been overwritten by the SKIMS turn.
"""

from __future__ import annotations

from app.agents import last_query
from app.agents.tools.search_products import _is_styleless_brand_query

# ─── P0-a: styleless brand-query detection ────────────────────────────────


def test_bare_brand_query_is_styleless():
    # "스킴스로 보여줘" → LLM emitted text_query="skims top women", brand="skims"
    assert _is_styleless_brand_query("skims top women", "skims", ["SKIMS"]) is True


def test_brand_only_no_gender_is_styleless():
    assert _is_styleless_brand_query("skims", "skims", ["SKIMS"]) is True


def test_brand_with_real_style_token_is_not_styleless():
    # "나이키 러닝화" → running shoes is a real descriptor → fresh search, no carry
    assert _is_styleless_brand_query("nike running shoes women", "nike", ["NIKE"]) is False


def test_multiword_brand_stripped():
    assert _is_styleless_brand_query("lack of color hat women", "lack of color", ["Lack of Color"]) is False
    assert _is_styleless_brand_query("lack of color women", "lack of color", ["Lack of Color"]) is True


def test_filler_words_are_styleless():
    assert _is_styleless_brand_query("show me some skims items", "skims", ["SKIMS"]) is True


# ─── P2: recent-search ring buffer ────────────────────────────────────────


def setup_function(_):
    last_query._reset_all_for_tests()


def test_ring_records_and_returns_newest_first():
    last_query.push_recent_search(1, "black cropped hoodie women", label="닝닝 공항패션")
    last_query.push_recent_search(1, "skims top women", brand=["SKIMS"], label="스킴스로")
    rows = last_query.get_recent_searches(1)
    assert [r["label"] for r in rows] == ["스킴스로", "닝닝 공항패션"]
    assert rows[0]["brand"] == ["SKIMS"]
    assert rows[1]["brand"] is None


def test_ring_same_turn_label_updates_in_place():
    # main persist then brand-pivot re-persist within one dispatch (same label)
    last_query.push_recent_search(2, "skims top women", label="스킴스로")
    last_query.push_recent_search(2, "skims top women black cropped hoodie", brand=["SKIMS"], label="스킴스로")
    rows = last_query.get_recent_searches(2)
    assert len(rows) == 1
    assert rows[0]["q"] == "skims top women black cropped hoodie"
    assert rows[0]["brand"] == ["SKIMS"]


def test_ring_caps_at_max():
    for i in range(last_query._RECENT_MAX + 3):
        last_query.push_recent_search(3, f"query {i}", label=f"turn {i}")
    rows = last_query.get_recent_searches(3)
    assert len(rows) == last_query._RECENT_MAX
    # oldest dropped, newest kept
    assert rows[0]["label"] == f"turn {last_query._RECENT_MAX + 2}"


def test_ring_empty_query_ignored():
    last_query.push_recent_search(4, "   ", label="noop")
    assert last_query.get_recent_searches(4) == []


def test_ring_survives_clear_last_query():
    # clear_last_query drops the single-slot anchor but the ring is a longer
    # history and must persist so "다시 <topic>" still resolves.
    last_query.push_recent_search(5, "black cropped hoodie women", label="닝닝")
    last_query.clear_last_query(5)
    assert last_query.get_recent_searches(5)[0]["label"] == "닝닝"


# ─── 색 재고부족 정직 안내 (color-relax notice) ────────────────────────────
# "핑크로 바꿨어!"라고 거짓 확답하던 버그: 색 필터가 재고부족으로 relax·drop돼도
# 그 사실이 에이전트에 전파 안 됨. color_relax_ctx → _build_color_notice.

from app.agents.tools.search_products import _build_color_notice  # noqa: E402
from app.services.search_service import color_relax_ctx  # noqa: E402


def test_color_notice_none_when_no_relax():
    color_relax_ctx.set(None)
    assert _build_color_notice() is None


def test_color_notice_zero_exact_is_honest_unavailable():
    color_relax_ctx.set({"requested_color": "PINK", "exact_count": 0, "subcategory_also_relaxed": False})
    note = _build_color_notice()
    assert note is not None
    assert "PINK" in note
    assert "no_exact_color" in note
    assert "do NOT claim" in note


def test_color_notice_low_exact_mentions_limited():
    color_relax_ctx.set({"requested_color": "GREEN", "exact_count": 4, "subcategory_also_relaxed": False})
    note = _build_color_notice()
    assert note is not None
    assert "low_exact_color" in note
    assert "GREEN" in note


def test_color_notice_subcat_relaxed_does_not_blame_color():
    color_relax_ctx.set({"requested_color": "PINK", "exact_count": 0, "subcategory_also_relaxed": True})
    note = _build_color_notice()
    assert note is not None
    assert "scarce_match" in note


def test_color_notice_missing_color_returns_none():
    color_relax_ctx.set({"exact_count": 0})
    assert _build_color_notice() is None


# ─── 결과 속성분포 digest (데이드림 벤치마크) ──────────────────────────────
# respond 가 "대부분 미디에 린넨" 처럼 사실 기반으로 묘사하도록 결과셋을 집계.

from app.agents.tools.search_products import _build_result_digest  # noqa: E402


def _cand(pid, brand, price, sub, meta):
    return {"id": pid, "brand": brand, "price": price, "subcategory": sub, "feature_metadata": meta}


async def test_digest_none_for_empty():
    assert await _build_result_digest([]) is None


async def test_digest_aggregates_dominant_attributes():
    cands = [
        _cand(
            1,
            "SKIMS",
            29000,
            "hoodie",
            {"fit": "relaxed", "pattern": "solid", "primary_color": "BLACK", "material": ["cotton"]},
        ),
        _cand(
            2,
            "SKIMS",
            49000,
            "hoodie",
            {"fit": "relaxed", "pattern": "solid", "primary_color": "BLACK", "material": ["cotton", "fleece"]},
        ),
        _cand(
            3,
            "Norba",
            140000,
            "hoodie",
            {"fit": "relaxed", "pattern": "solid", "primary_color": "GREY", "material": ["cotton"]},
        ),
    ]
    d = await _build_result_digest(cands)
    assert d is not None
    assert d["mostly"] == "hoodie"
    assert d["fit"] == "relaxed"  # 3/3 dominant
    assert "cotton" in d["materials"]  # top material
    assert "black" in d["colors"]  # 2/3 ≥ 0.25
    assert d["price_krw"] == "29,000–140,000"
    assert d["brands"].startswith("SKIMS")
    assert "pattern" not in d  # solid 은 생략


async def test_digest_omits_mixed_axes():
    # fit 이 제각각 → dominant 없음 → fit 키 생략(지어내기 방지)
    cands = [
        _cand(1, "A", 10000, "dress", {"fit": "slim", "primary_color": "RED", "material": ["silk"]}),
        _cand(2, "B", 20000, "skirt", {"fit": "oversized", "primary_color": "BLUE", "material": ["denim"]}),
        _cand(3, "C", 30000, "pants", {"fit": "relaxed", "primary_color": "GREEN", "material": ["linen"]}),
    ]
    d = await _build_result_digest(cands)
    assert d is not None
    assert "fit" not in d  # 1/3 each → below 0.5
    assert "mostly" not in d  # subcats all distinct → below 0.34
    assert d["price_krw"] == "10,000–30,000"
