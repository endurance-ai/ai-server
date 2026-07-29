"""v6 정밀 필터 (p_subcategory / p_color_family) 플럼빙 + 완화 재시도 테스트.

2026-07-15 — 백엔드 products.subcategory(60%+)/color(100%) 정규화 연동.
실물 RPC(7-param, p_color_family 신설)는 두 필터를 어느 rung 에서도
완화하지 않으므로, AI 서버 측 규칙을 고정한다:
  1. canonical vocab 매칭 성공 값만 전달 (미인식 → None, fail-open);
  2. subcategory resolve 시 family gate 를 그 소속 family 로 정렬
     (Vision hoodie=Outer vs 백엔드 hoodie→tops 불일치 → 교집합 0 방지);
  3. strict 결과 < SEARCH_FILTER_RELAX_MIN 이면 필터 제거 재시도 후
     id-dedup 병합 (strict 우선).
"""

from __future__ import annotations

from app.core.config import settings
from app.infrastructure.repositories.search_repository import SearchRepository
from app.models.request import AnalyzedItem, RecommendRequest
from app.pipeline.state import PipelineState
from app.services.search_service import search_service

_EMBED = [0.1] * 8


def _state(
    *,
    category: str = "apparel",
    subcategory: str | None = None,
    color_family: str | None = None,
    gender: str | None = None,
) -> PipelineState:
    item = AnalyzedItem(
        id="item-1",
        category=category,
        subcategory=subcategory,
        colorFamily=color_family,
        searchQuery="q",
    )
    req = RecommendRequest(item=item, imageUrl="https://example.com/x.jpg", gender=gender)
    state = PipelineState(request=req)
    state.embedding = list(_EMBED)
    return state


class _RpcSpy:
    def __init__(self, results: list[list[dict]]):
        self.calls: list[dict] = []
        self._results = results

    async def __call__(self, params: dict) -> list[dict]:
        self.calls.append(dict(params))
        idx = min(len(self.calls) - 1, len(self._results) - 1)
        return list(self._results[idx])


def _install(monkeypatch, spy: _RpcSpy) -> None:
    monkeypatch.setattr(SearchRepository, "search", staticmethod(spy))
    monkeypatch.setattr(settings, "BRAND_2TOWER_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "PERSONALIZE_RERANK_ENABLED", False, raising=False)


def _rows(*ids: int) -> list[dict]:
    return [{"id": i, "brand": "b", "distance": 0.1 * i} for i in ids]


# ── build_params 7-key passthrough ──────────────────────────────────────────


def test_build_params_seven_keys_with_precision_filters():
    params = SearchRepository.build_params(
        embedding=_EMBED,
        brand_filter=None,
        category="hoodie",
        subcategory="hoodie",
        color_family="black",
    )
    assert params["p_subcategory"] == "hoodie"
    assert params["p_color_family"] == "black"
    assert params["p_category"] == "tops"


# ── subcategory → family 정렬 ───────────────────────────────────────────────


async def test_vision_outer_hoodie_gates_tops_family(monkeypatch):
    """Vision 분류(hoodie=Outer)와 백엔드 배치(hoodie→tops)가 달라도
    family gate 는 subcategory 소속 family 를 따른다."""
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    await search_service(_state(category="Outer", subcategory="hoodie"))
    assert spy.calls[0]["p_category"] == "tops"
    assert spy.calls[0]["p_subcategory"] == "hoodie"


async def test_llm_category_arg_derives_subcategory(monkeypatch):
    """순수 텍스트 턴: LLM category="hoodie" (subcategory 레벨 단어) →
    p_subcategory 파생 + family gate 보강."""
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    await search_service(_state(category="hoodie"))
    assert spy.calls[0]["p_category"] == "tops"
    assert spy.calls[0]["p_subcategory"] == "hoodie"


async def test_unrecognized_subcategory_fails_open(monkeypatch):
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    await search_service(_state(category="Top", subcategory="knit"))
    assert spy.calls[0]["p_subcategory"] is None
    assert spy.calls[0]["p_category"] == "tops"  # category 경로 유지


# ── color canonical passthrough ─────────────────────────────────────────────


async def test_color_family_passed_through_uppercase(monkeypatch):
    """색 출처가 product_features.primary_color 로 이관되면서(2026-07-29)
    Vision color_family 를 그대로 넘기면 맞는다 — 구 _COLOR_ALIAS
    (multi→multicolor, gray→grey) 땜빵은 제거됨. RPC 가 `= UPPER(...)` 로
    비교하므로 대문자 canonical 표기로 전달한다."""
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    await search_service(_state(category="Top", color_family="MULTI"))
    assert spy.calls[0]["p_color_family"] == "MULTI"


async def test_color_family_lowercase_input_normalized(monkeypatch):
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    await search_service(_state(category="Top", color_family="grey"))
    assert spy.calls[0]["p_color_family"] == "GREY"


# ── 완화 재시도 ─────────────────────────────────────────────────────────────


async def test_relax_retry_when_strict_scarce(monkeypatch):
    """strict(정밀 필터) 결과가 RELAX_MIN 미만이면 필터 제거 재시도 후
    id-dedup 병합 (strict 행 우선)."""
    spy = _RpcSpy([_rows(1, 2), _rows(2, 3, 4, 5, 6)])
    _install(monkeypatch, spy)
    monkeypatch.setattr(settings, "SEARCH_FILTER_RELAX_MIN", 5)
    state = await search_service(_state(category="hoodie", color_family="black"))
    assert len(spy.calls) == 2
    assert spy.calls[0]["p_subcategory"] == "hoodie"
    assert spy.calls[1]["p_subcategory"] is None
    assert spy.calls[1]["p_color_family"] is None
    assert [r["id"] for r in state.raw_candidates] == [1, 2, 3, 4, 5, 6]


async def test_no_retry_when_strict_sufficient(monkeypatch):
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    monkeypatch.setattr(settings, "SEARCH_FILTER_RELAX_MIN", 5)
    await search_service(_state(category="hoodie"))
    assert len(spy.calls) == 1


async def test_no_retry_without_precision_filters(monkeypatch):
    """필터를 아예 안 태운 검색은 결과가 0 이어도 재시도하지 않는다
    (기존 raw_count=0 graceful 경로 보존)."""
    spy = _RpcSpy([[]])
    _install(monkeypatch, spy)
    await search_service(_state(category="Top"))
    assert len(spy.calls) == 1


# ── kill-switch 플래그 ──────────────────────────────────────────────────────


async def test_subcategory_flag_off_restores_none(monkeypatch):
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    monkeypatch.setattr(settings, "SEARCH_SUBCATEGORY_FILTER_ENABLED", False)
    await search_service(_state(category="hoodie", subcategory="hoodie"))
    assert spy.calls[0]["p_subcategory"] is None
    # flag off → family 정렬도 비활성: category 는 원래 경로(alias hoodie→tops)
    assert spy.calls[0]["p_category"] == "tops"


async def test_color_flag_off_restores_none(monkeypatch):
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    monkeypatch.setattr(settings, "SEARCH_COLOR_FILTER_ENABLED", False)
    await search_service(_state(category="Top", color_family="black"))
    assert spy.calls[0]["p_color_family"] is None


# ── p_gender 하드 필터 (2026-07-16) ─────────────────────────────────────────


async def test_gender_men_passes_to_rpc(monkeypatch):
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    await search_service(_state(category="Top", gender="men"))
    assert spy.calls[0]["p_gender"] == "men"


async def test_gender_unisex_maps_to_none(monkeypatch):
    """'unisex'/미확인은 필터 off — RPC 가 [g,'unisex'] 오버랩 매치라
    unisex 를 그대로 보내면 men/women 상품이 전부 배제되기 때문."""
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    await search_service(_state(category="Top", gender="unisex"))
    assert spy.calls[0]["p_gender"] is None


async def test_gender_unknown_value_fails_open(monkeypatch):
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    await search_service(_state(category="Top", gender="kids"))
    assert spy.calls[0]["p_gender"] is None


async def test_gender_flag_off_restores_none(monkeypatch):
    spy = _RpcSpy([_rows(1, 2, 3, 4, 5)])
    _install(monkeypatch, spy)
    monkeypatch.setattr(settings, "SEARCH_GENDER_FILTER_ENABLED", False)
    await search_service(_state(category="Top", gender="women"))
    assert spy.calls[0]["p_gender"] is None


async def test_relax_retry_preserves_gender(monkeypatch):
    """완화 재시도는 subcategory/color 만 제거 — gender 는 시맨틱 제약이라
    유지된다 (여성 요청에 남성 상품으로 리콜을 채우면 안 됨)."""
    spy = _RpcSpy([_rows(1, 2), _rows(2, 3, 4, 5, 6)])
    _install(monkeypatch, spy)
    monkeypatch.setattr(settings, "SEARCH_FILTER_RELAX_MIN", 5)
    await search_service(_state(category="hoodie", gender="women"))
    assert len(spy.calls) == 2
    assert spy.calls[1]["p_subcategory"] is None
    assert spy.calls[1]["p_gender"] == "women"
