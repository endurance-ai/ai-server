"""맨-브랜드 라우터의 '브랜드 + 품목' 처리와 속성어 우선 규칙 (2026-09-25).

9월 실유저 세션에서 드러난 문제:
- "스웨이드 자켓" → 별칭 '스웨이드'가 브랜드 SUADE 로 잡혀 SUADE 바지·티만 반환.
- "자라 아우터" / "팔로마 울 가방" / "마르지엘라 보스턴백" → 품목이 필터로 안 걸려
  브랜드 전 상품(원피스·신발·장갑)과 다른 브랜드 필러가 섞여 나옴.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agents import react_loop
from app.agents.tools import respond as respond_mod
from app.agents.tools import search_products as sp_mod
from app.infrastructure.repositories import brand_node_cache
from app.infrastructure.repositories.brand_node_cache import (
    _surface_keys,
    is_attribute_word,
    scan_text_for_brand,
)
from app.infrastructure.repositories.category_family import garment_family
from app.providers.database import DatabaseProvider


def _seed(monkeypatch, mapping: dict[str, list[str]]) -> None:
    filt: dict[str, list[str]] = {}
    for surface, names in mapping.items():
        for key in _surface_keys(surface):
            filt.setdefault(key, []).extend(names)
    monkeypatch.setattr(brand_node_cache, "_filter_index", filt)
    monkeypatch.setattr(brand_node_cache, "_acronym_index", {})


_CATALOG = {
    "SUADE": ["SUADE"],
    "스웨이드": ["SUADE"],
    "RRACE": ["RRACE"],
    "레이스": ["RRACE"],
    "ZARA": ["ZARA"],
    "자라": ["ZARA"],
    "Paloma Wool": ["Paloma Wool"],
    "팔로마 울": ["Paloma Wool"],
    "Maison Margiela": ["Maison Margiela"],
    "마르지엘라": ["Maison Margiela"],
}


def _state(text: str) -> Any:
    return SimpleNamespace(image_url=None, message=SimpleNamespace(text=text, callback_data=None))


# ── 속성어 우선 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("word", ["스웨이드", "레이스", "블랙", "suede", "린넨", "체크"])
def test_attribute_words_detected(word):
    assert is_attribute_word(word)


@pytest.mark.parametrize("word", ["자라", "마르지엘라", "자켓", "보여줘"])
def test_non_attribute_words(word):
    assert not is_attribute_word(word)


def test_material_wins_over_brand_alias_in_free_text(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    assert scan_text_for_brand("스웨이드 자켓") is None
    assert scan_text_for_brand("레이스 원피스 보여줘") is None


def test_explicit_brand_label_keeps_brand(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    assert scan_text_for_brand("스웨이드 브랜드 제품 추천") == ["SUADE"]
    assert scan_text_for_brand("브랜드 스웨이드 보여줘") == ["SUADE"]


def test_attribute_does_not_hijack_other_brand(monkeypatch):
    # "자라 스웨이드 자켓": 브랜드는 자라, 스웨이드는 속성 → 단순 브랜드 요청 아님(LLM 경로).
    _seed(monkeypatch, _CATALOG)
    assert scan_text_for_brand("자라 스웨이드 자켓") == ["ZARA"]
    assert react_loop._detect_bare_brand_request(_state("자라 스웨이드 자켓"), None) is None


def test_suede_jacket_not_routed_as_bare_brand(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    assert react_loop._detect_bare_brand_request(_state("스웨이드 자켓"), None) is None


# ── 품목어 → family ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("token", "family"),
    [
        ("아우터", "outerwear"),
        ("자켓", "outerwear"),
        ("가방", "bags"),
        ("보스턴백", "bags"),
        ("후드집업", "tops"),
        ("레더자켓", "outerwear"),
        ("원피스", "dresses"),
        ("jacket", "outerwear"),
        ("bag", "bags"),
    ],
)
def test_garment_family(token, family):
    assert garment_family(token) == family


@pytest.mark.parametrize("token", ["세일", "신상", "clothing", "", None])
def test_non_garment_returns_none(token):
    assert garment_family(token) is None


@pytest.mark.parametrize(
    ("text", "brand", "family", "garment"),
    [
        ("자라 아우터", "ZARA", "outerwear", "아우터"),
        ("팔로마 울 가방", "Paloma Wool", "bags", "가방"),
        ("마르지엘라 보스턴백", "Maison Margiela", "bags", "보스턴백"),
    ],
)
def test_detect_brand_plus_garment(monkeypatch, text, brand, family, garment):
    _seed(monkeypatch, _CATALOG)
    req = react_loop._detect_bare_brand_request(_state(text), None)
    assert req is not None
    assert req["brand"] == brand
    assert req["family"] == family
    assert req["garment"] == garment


def test_detect_brand_only_has_no_family(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    req = react_loop._detect_bare_brand_request(_state("자라 보여줘"), None)
    assert req is not None
    assert req["family"] is None


# ── 라우터 실행: 있으면 브랜드+family, 없으면 '없다' + 유사 브랜드 같은 품목 ─────


def _wire(monkeypatch, has: bool | None, cnt: int = 12) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake_has(names, family, gender=None):
        seen["has_args"] = (names, family, gender)
        return has

    async def fake_search(args, ctx):
        seen["search_args"] = args
        return {"ok": True, "candidates_count": cnt}

    async def fake_respond(args, ctx):
        seen["text"] = args["text"]
        return {"ok": True}

    monkeypatch.setattr(DatabaseProvider, "brand_has_family", staticmethod(fake_has))
    monkeypatch.setattr(sp_mod, "dispatch", fake_search)
    monkeypatch.setattr(respond_mod, "dispatch", fake_respond)
    monkeypatch.setattr(sp_mod, "_lookup_profile_gender", lambda ctx: None)
    return seen


def _req(brand: str, label: str, family: str, garment: str) -> dict[str, Any]:
    return {"brand": brand, "label": label, "family": family, "garment": garment, "text_query": garment}


@pytest.mark.asyncio
async def test_garment_in_catalog_filters_by_family(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    seen = _wire(monkeypatch, has=True)
    out = await react_loop._run_bare_brand_shortcircuit(
        _req("ZARA", "자라", "outerwear", "아우터"), _state("자라 아우터"), None, {"lang": "ko"}
    )
    assert out is not None
    assert seen["search_args"]["brand"] == "ZARA"
    assert seen["search_args"]["category"] == "outerwear"
    assert "append_similar_brand" not in seen["search_args"]
    assert seen["text"].startswith("자라 아우터")


@pytest.mark.asyncio
async def test_garment_missing_says_so_and_shows_similar_brands(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    seen = _wire(monkeypatch, has=False)
    out = await react_loop._run_bare_brand_shortcircuit(
        _req("Paloma Wool", "팔로마 울", "bags", "가방"), _state("팔로마 울 가방"), None, {"lang": "ko"}
    )
    assert out is not None
    assert seen["search_args"]["similar_to_brand"] == "Paloma Wool"
    assert seen["search_args"]["category"] == "bags"
    assert "brand" not in seen["search_args"]
    assert "없어" in seen["text"]


@pytest.mark.asyncio
async def test_family_check_failure_falls_back_to_loop(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    seen = _wire(monkeypatch, has=None)
    out = await react_loop._run_bare_brand_shortcircuit(
        _req("ZARA", "자라", "outerwear", "아우터"), _state("자라 아우터"), None, {"lang": "ko"}
    )
    assert out is None
    assert "search_args" not in seen


@pytest.mark.asyncio
async def test_empty_result_falls_back_to_loop(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    seen = _wire(monkeypatch, has=False, cnt=0)
    out = await react_loop._run_bare_brand_shortcircuit(
        _req("Paloma Wool", "팔로마 울", "bags", "가방"), _state("팔로마 울 가방"), None, {"lang": "ko"}
    )
    assert out is None
    assert "text" not in seen


@pytest.mark.asyncio
async def test_family_check_uses_request_gender(monkeypatch):
    _seed(monkeypatch, _CATALOG)
    seen = _wire(monkeypatch, has=True)
    await react_loop._run_bare_brand_shortcircuit(
        _req("ZARA", "자라", "outerwear", "아우터"), _state("자라 아우터"), None, {"lang": "ko", "req_gender": "men"}
    )
    assert seen["has_args"] == (["ZARA"], "outerwear", "men")


@pytest.mark.asyncio
async def test_narrow_garment_is_described_honestly(monkeypatch):
    # family 필터는 '가방' 단위 — '보스턴백'만 골랐다고 말하지 않는다.
    _seed(monkeypatch, _CATALOG)
    seen = _wire(monkeypatch, has=True)
    await react_loop._run_bare_brand_shortcircuit(
        _req("Maison Margiela", "마르지엘라", "bags", "보스턴백"), _state("마르지엘라 보스턴백"), None, {"lang": "ko"}
    )
    assert seen["text"].startswith("마르지엘라 가방 중에서 보스턴백에 가까운")
