"""search_products — 상품명 직접 지목 (2026-09-29).

운영 로그에서 사용자가 카드의 상품명을 핀 없이 그대로 적어 보낸 턴 두 건:

- 9/27 "Lace Long-Sleeve Blouse 찾아줘" (Rabanne 재고 상품명 그대로). 에이전트는
  이름을 버리고 속성으로만 검색했고, 앞 턴 사진에서 온 color_family=grey 게이트가
  검정인 그 상품을 걸러냈다.
- 9/21 "FIT JERSEY [BLACK]랑 비슷한 스타일 찾아줘" (직전 턴 카드 3번째 SUADE 상품).
  "비슷한"이므로 핀 상품과 같은 유사상품 앵커가 맞는데, 속성 검색만 했다.

정확 지목이면 name_query 로 보내고 색 게이트를 끄고, "비슷한" 류면 그 상품 임베딩을
앵커로 쓰는지 고정한다.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.agents.tools.search_products as sp

_DIM = 768
_ANCHOR_VEC = [0.42] * _DIM
_TEXT_VEC = [0.11] * _DIM


@pytest.fixture
def _mock_embed_text(monkeypatch):
    mock = AsyncMock(return_value=_TEXT_VEC)
    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", mock)
    return mock


@pytest.fixture
def _captured_search(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_search_step(state):
        item = state.request.item
        captured["embedding"] = list(state.embedding or [])
        captured["category"] = item.category
        captured["subcategory"] = item.subcategory
        captured["color_family"] = item.color_family
        captured["name_query"] = item.name_query
        captured["search_query"] = item.search_query
        state.raw_candidates = [{"id": "p1", "name": "X", "brand": "Y", "distance": 0.2}]
        return state

    async def fake_diversify(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.pipeline.search.search_step", fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify)
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda ctx: "women")
    return captured


@pytest.fixture
def _session(monkeypatch):
    sess = SimpleNamespace(last_results=[])
    store = SimpleNamespace(get_or_create=lambda chat_id: sess)
    monkeypatch.setattr("app.infrastructure.memory.session.get_store", lambda: store)
    return sess


@pytest.mark.asyncio
async def test_exact_product_name_goes_to_name_query_without_color_gate(
    monkeypatch, _mock_embed_text, _captured_search, _session
):
    """9/27 재현: 사진 다음 턴에 상품명을 그대로 적음 → name_query 로 그 이름,
    앞 턴 사진에서 온 grey 색 게이트와 Vision 카테고리는 쓰지 않는다."""
    find = AsyncMock(
        return_value=[{"id": 448669, "name": "lace long-sleeve blouse", "brand": "Rabanne", "category": "tops"}]
    )
    monkeypatch.setattr("app.providers.database.DatabaseProvider.find_in_stock_products_by_name", find)

    ctx = {
        "chat_id": 7,
        "user_key": "u:7",
        "image_url": "",
        # 앞 턴 사진의 Vision 검색어가 text_query 에 남아 있는 상태
        "text_query": "relaxed long-sleeve crew-neck charcoal grey semi-sheer lace blouse women",
        "user_msg": "Lace Long-Sleeve Blouse 찾아줘",
        "vision_category": "blouse",
        "vision_subcategory": "blouse",
    }
    args = {
        "text_query": "grey fitted long sleeve crew neck lace blouse women",
        "category": "blouse",
        "color_family": "grey",
        "material": "lace",
    }
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    find.assert_awaited_once_with("Lace Long-Sleeve Blouse")
    assert _captured_search["name_query"] == "lace long-sleeve blouse"
    # 검색 문장도 에이전트의 속성 문장이 아니라 상품명으로 바뀐다(성별 토큰은 뒤에 붙을 수 있음)
    assert str(_captured_search["search_query"]).startswith("lace long-sleeve blouse")
    assert _captured_search["color_family"] is None
    assert _captured_search["category"] == "tops"
    assert _captured_search["subcategory"] is None


@pytest.mark.asyncio
async def test_similar_to_named_card_anchors_on_that_product(monkeypatch, _mock_embed_text, _captured_search, _session):
    """9/21 재현: 직전 카드의 상품명 + '비슷한' → 핀과 같은 상품 임베딩 앵커."""
    _session.last_results = [
        SimpleNamespace(id="770665", name="Other Top", brand="LESET"),
        SimpleNamespace(id="855565", name="FIT JERSEY [BLACK]", brand="SUADE"),
    ]
    find = AsyncMock(return_value=[])
    fetch_emb = AsyncMock(return_value=_ANCHOR_VEC)
    fetch_cat = AsyncMock(return_value="tops")
    monkeypatch.setattr("app.providers.database.DatabaseProvider.find_in_stock_products_by_name", find)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_embedding", fetch_emb)
    monkeypatch.setattr("app.providers.database.DatabaseProvider.get_product_category", fetch_cat)

    ctx = {
        "chat_id": 8,
        "user_key": "u:8",
        "image_url": "",
        "text_query": "FIT JERSEY [BLACK]랑 비슷한 스타일 찾아줘",
        "user_msg": "FIT JERSEY [BLACK]랑 비슷한 스타일 찾아줘",
    }
    args = {"text_query": "black fitted long sleeve top women", "category": "top", "color_family": "black"}
    res = await sp.dispatch(args, ctx)

    assert res["ok"] is True
    find.assert_not_awaited()  # 직전 카드에서 찾았으니 카탈로그 조회 없음
    fetch_emb.assert_awaited_once_with(855565)
    _mock_embed_text.assert_not_awaited()
    assert _captured_search["embedding"] == _ANCHOR_VEC
    assert _captured_search["name_query"] is None


@pytest.mark.parametrize(
    "user_msg",
    [
        "Auralee",  # 한 단어 영문은 브랜드명일 때가 많아 후보로 보지 않음
        "하객룩 원피스",  # 영문 구간 없음
        "black",  # 한 단어 · 6자 미만
    ],
)
@pytest.mark.asyncio
async def test_non_product_messages_skip_lookup(monkeypatch, _mock_embed_text, _captured_search, _session, user_msg):
    find = AsyncMock(return_value=[])
    monkeypatch.setattr("app.providers.database.DatabaseProvider.find_in_stock_products_by_name", find)
    ctx = {"chat_id": 9, "user_key": "u:9", "image_url": "", "text_query": user_msg, "user_msg": user_msg}
    res = await sp.dispatch({"text_query": "dress", "color_family": "black"}, ctx)

    assert res["ok"] is True
    find.assert_not_awaited()
    assert _captured_search["name_query"] is None
    assert _captured_search["color_family"] == "black"


@pytest.mark.asyncio
async def test_same_name_in_several_brands_is_left_alone(monkeypatch, _mock_embed_text, _captured_search, _session):
    """이름이 같은 상품이 여러 브랜드에 있으면 어느 상품인지 모르므로 손대지 않는다."""
    find = AsyncMock(
        return_value=[
            {"id": 1, "name": "Oversized Beige Trench Coat", "brand": "A", "category": "outerwear"},
            {"id": 2, "name": "Oversized Beige Trench Coat", "brand": "B", "category": "outerwear"},
        ]
    )
    monkeypatch.setattr("app.providers.database.DatabaseProvider.find_in_stock_products_by_name", find)
    msg = "Oversized Beige Trench Coat 코트"
    ctx = {"chat_id": 10, "user_key": "u:10", "image_url": "", "text_query": msg, "user_msg": msg}
    res = await sp.dispatch({"text_query": "beige trench coat", "color_family": "beige"}, ctx)

    assert res["ok"] is True
    assert _captured_search["name_query"] is None
    assert _captured_search["color_family"] == "beige"


@pytest.mark.asyncio
async def test_no_catalog_match_keeps_agent_args(monkeypatch, _mock_embed_text, _captured_search, _session):
    find = AsyncMock(return_value=[{"id": 3, "name": "Kiko Kostadinov Mens Jacket", "brand": "Kiko", "category": "x"}])
    monkeypatch.setattr("app.providers.database.DatabaseProvider.find_in_stock_products_by_name", find)
    # 두 단어 이상 영문이라 조회는 하지만, 이름이 정확히 같은 상품이 없으면 그대로 둔다.
    msg = "Kiko kostadinov mens 제품 25ss 이전 제품 확인해줘"
    ctx = {"chat_id": 11, "user_key": "u:11", "image_url": "", "text_query": msg, "user_msg": msg}
    res = await sp.dispatch({"text_query": "jacket", "color_family": "black"}, ctx)

    assert res["ok"] is True
    assert _captured_search["name_query"] is None
    assert _captured_search["color_family"] == "black"
