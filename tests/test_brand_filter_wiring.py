"""브랜드 지정 요청 배선 테스트 (2026-07-16).

LLM `brand` arg → `_resolve_brand_filter` → canonical brand_name →
`RecommendRequest.brand_filter` → `p_brand_names` (EXACT 매치). RPC 가
`brand_nodes.brand_name = ANY(...)` 라 canonical 명만 유효 — 미인식 브랜드는
None(fail-open: text_query 임베딩의 soft 신호로만 작동)이어야 한다.
"""

from __future__ import annotations

from app.agents.tools.search_products import _recover_pinned_brand, _resolve_brand_filter
from app.infrastructure.memory.session import get_store
from app.infrastructure.repositories import brand_node_cache
from app.infrastructure.repositories.brand_node_cache import _surface_keys, scan_text_for_brand


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


# ── clarify 연속 턴 브랜드 핀 (_recover_pinned_brand) ────────────────────────


def _seed_last_results(chat_id: int, brands: list[str]) -> None:
    """세션 last_results 에 brand 텍스트만 가진 후보를 심는다."""
    store = get_store()
    sess = store.get_or_create(chat_id)
    sess.last_results = [{"id": str(i), "brand": b} for i, b in enumerate(brands)]
    store.update(sess)


def test_pin_recovers_single_brand_from_last_results(monkeypatch):
    _seed_cache(monkeypatch, "GLOWNY")
    _seed_last_results(4101, ["GLOWNY", "GLOWNY", "GLOWNY", "GLOWNY"])
    assert _recover_pinned_brand({"chat_id": 4101}) == ["GLOWNY"]


def test_pin_skips_mixed_brands(monkeypatch):
    _seed_cache(monkeypatch, "GLOWNY", "Acne Studios")
    _seed_last_results(4102, ["GLOWNY", "Acne Studios", "GLOWNY", "Acne Studios"])
    assert _recover_pinned_brand({"chat_id": 4102}) is None


def test_pin_skips_when_too_few_results(monkeypatch):
    _seed_cache(monkeypatch, "GLOWNY")
    _seed_last_results(4103, ["GLOWNY", "GLOWNY"])  # < 3 → 추론 불가
    assert _recover_pinned_brand({"chat_id": 4103}) is None


def test_pin_dominant_brand_over_80pct(monkeypatch):
    _seed_cache(monkeypatch, "GLOWNY", "Acne Studios")
    # 9/10 GLOWNY → ≥80% → 핀.
    _seed_last_results(4104, ["GLOWNY"] * 9 + ["Acne Studios"])
    assert _recover_pinned_brand({"chat_id": 4104}) == ["GLOWNY"]


def test_pin_no_chat_id(monkeypatch):
    _seed_cache(monkeypatch, "GLOWNY")
    assert _recover_pinned_brand({}) is None


# ── 자유 문장 브랜드 스캔 (scan_text_for_brand) ──────────────────────────────


def _seed_alias_index(monkeypatch, mapping: dict[str, list[str]]) -> None:
    """surface(별칭 포함) → canonical 명 리스트로 _filter_index 직접 구성.
    한글 별칭('글로니' → GLOWNY)은 실제로 brand_aliases 에서 오지만, 스캔
    테스트에선 표면형 키만 있으면 되므로 여기서 직접 심는다."""
    filt: dict[str, list[str]] = {}
    for surface, names in mapping.items():
        for key in _surface_keys(surface):
            filt.setdefault(key, []).extend(names)
    monkeypatch.setattr(brand_node_cache, "_filter_index", filt)
    monkeypatch.setattr(brand_node_cache, "_acronym_index", {})


def test_scan_finds_brand_in_free_text(monkeypatch):
    _seed_alias_index(monkeypatch, {"GLOWNY": ["GLOWNY"], "글로니": ["GLOWNY"]})
    assert scan_text_for_brand("글로니 제품 찾아줘") == ["GLOWNY"]
    assert scan_text_for_brand("글로니 상의 보여줘") == ["GLOWNY"]


def test_scan_none_when_no_brand(monkeypatch):
    _seed_alias_index(monkeypatch, {"GLOWNY": ["GLOWNY"], "글로니": ["GLOWNY"]})
    assert scan_text_for_brand("상의") is None
    assert scan_text_for_brand("여름 원피스 추천") is None
    assert scan_text_for_brand("") is None
    assert scan_text_for_brand(None) is None


def test_scan_prefers_longest_multiword_match(monkeypatch):
    _seed_cache(monkeypatch, "Acne Studios")
    # 2-어절 브랜드명이 문장 안에 있으면 잡는다.
    assert scan_text_for_brand("acne studios 니트 보여줘") == ["Acne Studios"]


def test_scan_skips_common_word_aliases(monkeypatch):
    # '키스'(Kith)처럼 일상어와 겹치는 1어절 별칭은 오탐 방지로 스캔 제외.
    _seed_cache(monkeypatch, "키스")
    assert scan_text_for_brand("키스 하고 싶은 원피스") is None


# --- bare-brand + similar 병합 (2026-09-13) ------------------------------------


def test_merge_brand_similar_head_then_similar_excludes_seed():
    from app.agents.tools.search_products import _merge_brand_similar

    head = [{"id": "1", "brand": "OJOS"}, {"id": "2", "brand": "OJOS"}]
    sim = [
        {"id": "2", "brand": "OJOS"},  # dedup (이미 head)
        {"id": "9", "brand": "OJOS"},  # seed 브랜드 → 제외
        {"id": "3", "brand": "GLOWNY"},
        {"id": "4", "brand": "999HUMANITY"},
    ]
    out = _merge_brand_similar(head, sim, {"ojos"}, top_k=10)
    assert [c["id"] for c in out] == ["1", "2", "3", "4"]
    assert all(c["brand"] != "OJOS" for c in out[2:])


def test_merge_brand_similar_respects_top_k():
    from app.agents.tools.search_products import _merge_brand_similar

    head = [{"id": "1", "brand": "OJOS"}]
    sim = [{"id": str(i), "brand": "X"} for i in range(10, 20)]
    out = _merge_brand_similar(head, sim, {"ojos"}, top_k=4)
    assert len(out) == 4
    assert out[0]["id"] == "1"


# --- 웹 최소 카드 보장 _ensure_min_web_cards (2026-09-21) --------------------


async def test_web_min_cards_noop_when_enough():
    import app.agents.tools.search_products as sp

    cands = [{"id": str(i), "brand": "A"} for i in range(10)]
    out = await sp._ensure_min_web_cards(cands, minimum=10, top_k=50, text_query="x", gender=None, user_key=None)
    assert out is cands  # 이미 충분 → 그대로


async def test_web_min_cards_centroid_backfill(monkeypatch):
    import app.agents.tools.search_products as sp
    from app.providers.database import DatabaseProvider

    async def fake_emb(pid):
        return [0.1, 0.2, 0.3, 0.4]

    monkeypatch.setattr(DatabaseProvider, "get_product_embedding", fake_emb)
    filler = [{"id": str(i), "brand": "X"} for i in range(3, 25)]

    async def fake_search(**kw):
        assert kw.get("override_embedding")  # centroid 앵커로 호출됨
        return filler

    monkeypatch.setattr(sp, "run_text_only_search", fake_search)
    cands = [{"id": "1", "brand": "A"}, {"id": "2", "brand": "B"}]
    out = await sp._ensure_min_web_cards(cands, minimum=10, top_k=50, text_query="x", gender=None, user_key=None)
    assert len(out) == 10
    assert [c["id"] for c in out[:2]] == ["1", "2"]  # 원본 앞
    assert len({c["id"] for c in out}) == 10  # dedup


async def test_web_min_cards_widen_when_zero(monkeypatch):
    import app.agents.tools.search_products as sp

    filler = [{"id": str(i), "brand": "X"} for i in range(30)]

    async def fake_search(**kw):
        assert "override_embedding" not in kw or kw.get("override_embedding") is None  # 게이트/앵커 없는 재검색
        return filler

    monkeypatch.setattr(sp, "run_text_only_search", fake_search)
    out = await sp._ensure_min_web_cards([], minimum=10, top_k=50, text_query="x", gender=None, user_key=None)
    assert len(out) == 10  # 0 이어도 무조건 채움
