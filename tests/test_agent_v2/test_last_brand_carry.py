"""직전 브랜드 필터 보관/복원 (2026-08-19).

refine("다른 색상으로")가 직전 검색의 브랜드(예: 아크네)를 잃고 다른 브랜드
상품을 뽑던 버그. 브랜드를 텍스트와 별도로 last_query 모듈에 보관해 이어받는다.
"""

from __future__ import annotations

from app.agents.last_query import (
    clear_last_query,
    get_last_brand,
    set_last_brand,
)


def test_set_get_brand_roundtrip() -> None:
    clear_last_query(777)
    set_last_brand(777, ["Acne Studios"])
    assert get_last_brand(777) == ["Acne Studios"]


def test_empty_brand_clears_prior() -> None:
    # 브랜드 없는 검색 후엔 stale 브랜드가 다음 refine 에 새면 안 된다.
    set_last_brand(778, ["Gucci"])
    set_last_brand(778, None)
    assert get_last_brand(778) is None
    set_last_brand(778, ["Gucci"])
    set_last_brand(778, [])
    assert get_last_brand(778) is None


def test_clear_last_query_also_clears_brand() -> None:
    set_last_brand(779, ["COS"])
    clear_last_query(779)
    assert get_last_brand(779) is None


def test_blank_entries_filtered() -> None:
    set_last_brand(780, ["  ", "Marithe", ""])
    assert get_last_brand(780) == ["Marithe"]
