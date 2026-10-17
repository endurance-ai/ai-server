"""원문 브랜드 핀 오탐 가드 (2026-10-17).

상품명 지목 e2e 측정(dev, P2 100건)에서 실패 44건 중 15건이 원문 브랜드 핀 오탐이었다.
별칭 캐시에 일반 단어 · 숫자와 겹치는 키가 있어 'in'→IS-NESS, 'the'→Tag Heuer Eyewear,
'5만원'→MARUN5, '2월'→Dsquared2, 'Off White'→Off-White, 'NERO'→Nero, '갤러리'→1011GALLERY 가
하드필터로 걸렸다. 최근 30일 user_text 151건에서도 같은 오탐 13건이 확인됐다(가격 조건
'5만원 이하'가 MARUN5 필터로 바뀌는 실제 발화 포함). 캐시는 그대로 두고 핀 스캔만 거른다.
"""

from __future__ import annotations

import pytest

from app.agents import react_loop
from app.infrastructure.repositories import brand_node_cache
from app.infrastructure.repositories.brand_node_cache import _surface_keys, scan_text_for_brand

_CATALOG = {
    "IS-NESS": ["IS-NESS"],
    "in": ["IS-NESS"],
    "Tag Heuer Eyewear": ["Tag Heuer Eyewear"],
    "the": ["Tag Heuer Eyewear"],
    "Dsquared2": ["Dsquared2"],
    "2": ["Dsquared2"],
    "MARUN5": ["MARUN5"],
    "5": ["MARUN5"],
    "Femme LA": ["Femme LA"],
    "la": ["Femme LA"],
    "Off-White": ["Off-White"],
    "Nero": ["Nero"],
    "1011갤러리": ["1011GALLERY"],
    "글로니": ["GLOWNY"],
    "자라": ["ZARA"],
    "The Row": ["The Row"],
}


@pytest.fixture(autouse=True)
def _seed(monkeypatch):
    filt: dict[str, list[str]] = {}
    for surface, names in _CATALOG.items():
        for key in _surface_keys(surface):
            filt.setdefault(key, []).extend(names)
    monkeypatch.setattr(brand_node_cache, "_filter_index", filt)
    monkeypatch.setattr(brand_node_cache, "_acronym_index", {})


@pytest.mark.parametrize(
    "text",
    [
        "jacket in black",
        "show me the bag",
        "2 piece set",
        "5만원 이하 니트",
        "2월 생일 선물",
        "la vie en rose dress",
        "off white sneakers",
        "nero leather bag",
        "스트리트 갤러리 티셔츠",
    ],
)
def test_common_words_do_not_pin(text):
    # 수정 전 핀 스캔(scan_text_for_brand)은 이 문장들을 브랜드로 잡았다.
    assert scan_text_for_brand(text) is not None
    assert react_loop._scan_brand_for_pin(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("글로니 보여줘", ["GLOWNY"]),
        ("자라 아우터", ["ZARA"]),
        ("the row bag", ["The Row"]),
        ("Off-White hoodie", ["Off-White"]),
        ("MARUN5 반지", ["MARUN5"]),
        ("Dsquared2 청바지", ["Dsquared2"]),
        ("1011갤러리 티셔츠", ["1011GALLERY"]),
        ("글로니 in black", ["GLOWNY"]),
    ],
)
def test_real_brands_still_pin(text, expected):
    assert react_loop._scan_brand_for_pin(text) == expected
