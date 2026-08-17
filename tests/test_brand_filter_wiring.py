"""브랜드 지정 요청 배선 테스트 (2026-07-16).

LLM `brand` arg → `_resolve_brand_filter` → canonical brand_name →
`RecommendRequest.brand_filter` → `p_brand_names` (EXACT 매치). RPC 가
`brand_nodes.brand_name = ANY(...)` 라 canonical 명만 유효 — 미인식 브랜드는
None(fail-open: text_query 임베딩의 soft 신호로만 작동)이어야 한다.
"""

from __future__ import annotations

from app.agents.tools.search_products import _resolve_brand_filter
from app.infrastructure.repositories import brand_node_cache
from app.infrastructure.repositories.brand_node_cache import _surface_keys


def _seed_cache(monkeypatch, *names: str) -> None:
    """Build the filter index the way warm_cache does (every Latin/Hangul
    surface key → the group's canonical name(s)), so the wiring test exercises
    `_resolve_brand_filter` against a realistic `_filter_index`."""
    filt: dict[str, list[str]] = {}
    for n in names:
        for key in _surface_keys(n):
            filt.setdefault(key, []).append(n)
    monkeypatch.setattr(brand_node_cache, "_filter_index", filt)
    monkeypatch.setattr(brand_node_cache, "_acronym_index", {})


def test_resolves_canonical_name_case_insensitive(monkeypatch):
    _seed_cache(monkeypatch, "Acne Studios")
    assert _resolve_brand_filter("acne studios") == ["Acne Studios"]
    assert _resolve_brand_filter("ACNE  Studios") == ["Acne Studios"]


def test_normalization_strips_punctuation(monkeypatch):
    _seed_cache(monkeypatch, "1017 ALYX 9SM")
    assert _resolve_brand_filter("1017 alyx 9sm") == ["1017 ALYX 9SM"]


def test_resolves_korean_surface_of_bilingual_name(monkeypatch):
    # brand_name carries both languages; a Korean-language query must resolve.
    _seed_cache(monkeypatch, "MONDAY EDITION (먼데이에디션)")
    assert _resolve_brand_filter("먼데이에디션") == ["MONDAY EDITION (먼데이에디션)"]
    assert _resolve_brand_filter("monday edition") == ["MONDAY EDITION (먼데이에디션)"]


def test_resolves_parenthetical_acronym(monkeypatch):
    _seed_cache(monkeypatch, "Post Archive Faction (PAF)")
    assert _resolve_brand_filter("paf") == ["Post Archive Faction (PAF)"]


def test_unknown_brand_fails_open(monkeypatch):
    _seed_cache(monkeypatch, "Acne Studios")
    assert _resolve_brand_filter("존재하지않는브랜드") is None


def test_empty_and_non_string_fail_open(monkeypatch):
    _seed_cache(monkeypatch, "Acne Studios")
    assert _resolve_brand_filter(None) is None
    assert _resolve_brand_filter("") is None
    assert _resolve_brand_filter("   ") is None
    assert _resolve_brand_filter(123) is None


def test_cold_cache_fails_open(monkeypatch):
    _seed_cache(monkeypatch)  # empty cache (워밍 실패 시나리오)
    assert _resolve_brand_filter("Acne Studios") is None
