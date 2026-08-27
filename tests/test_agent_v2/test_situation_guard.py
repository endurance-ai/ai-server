"""과확장 가드 — sub_queries(코디 확장)를 상황/TPO 쿼리로만 제한.

약한 모델이 단품 요청("neutral minimal jacket")에도 sub_queries 를 채워
풀코디+엉뚱 카테고리(재킷 검색에 토트백)로 새는 실패(트레이스 79ea6c4d) 방지.
"""

from __future__ import annotations

from app.agents.tools.search_products import _is_situation_query

# ── 상황/TPO → 확장 허용 (True) ──────────────────────────────────────────────


def test_situation_korean_occasion():
    assert _is_situation_query("결혼식 하객룩 추천해줘")
    assert _is_situation_query("데이트룩 뭐 입지")
    assert _is_situation_query("면접 볼 때 입을 거")


def test_situation_english_occasion():
    assert _is_situation_query("wedding guest outfit women")
    assert _is_situation_query("what to wear on a date")
    assert _is_situation_query("minimal office outfit")


# ── 단품 요청 → 확장 해제 (False) ────────────────────────────────────────────


def test_specific_garment_is_not_situation():
    # 실패 트레이스의 그 쿼리 — 확장되면 안 됨.
    assert not _is_situation_query("neutral minimal jacket men")
    assert not _is_situation_query("grey hoodie")
    assert not _is_situation_query("black satin mini dress")
    assert not _is_situation_query("오버핏 후드")


def test_empty_is_not_situation():
    assert not _is_situation_query("")
    assert not _is_situation_query(None)  # type: ignore[arg-type]


def test_word_boundary_no_false_positive():
    # 'date' 가 다른 단어 안에 있어도(예: update) 오탐하지 않는다.
    assert not _is_situation_query("update my search")
    # 'guest' 없는 일반 쿼리.
    assert not _is_situation_query("beige knit sweater")
