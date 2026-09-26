"""'X처럼/같은' 비교 브랜드는 검색 필터로 고정(pin)하지 않는다 (2026-09-27).

9/17 "이니어 후드집업처럼 얇고 가벼운 후드집업 … 룰루레몬이나 유니클로 처럼" →
별칭 '이니어'→innir 가 세션 브랜드 핀이 돼 이 턴의 모든 검색에 하드필터로 붙었다.
innir 남성 재고는 1개라 검색 4회(필터 없는 "hoodie men" 포함)가 전부 0건.
brand-similar 경로는 seed 를 제외하는데 핀이 같은 seed 를 필터로 얹어 항상 0건.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.agents.tools.search_products as sp
from app.agents import react_loop
from app.agents.last_query import clear_pinned_brand, get_pinned_brand, set_pinned_brand
from app.agents.react_loop import _build_ctx
from app.agents.tools import respond as respond_mod
from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.repositories import brand_node_cache
from app.infrastructure.repositories.brand_node_cache import _surface_keys
from app.infrastructure.repositories.search_repository import SearchRepository
from app.providers.database import DatabaseProvider

_CHAT = 424242


class _Sess:
    lang = "ko"
    detected_items: list = []
    vision_item = None


def _seed(monkeypatch) -> None:
    filt: dict[str, list[str]] = {}
    for surface, names in {
        "innir": ["innir"],
        "이니어": ["innir"],
        "유니클로": ["Uniqlo"],
        "Uniqlo": ["Uniqlo"],
    }.items():
        for key in _surface_keys(surface):
            filt.setdefault(key, []).extend(names)
    monkeypatch.setattr(brand_node_cache, "_filter_index", filt)
    monkeypatch.setattr(brand_node_cache, "_acronym_index", {})


def _state(text: str) -> WorkingState:
    msg = ChannelMessage(chat_id=_CHAT, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=_CHAT, from_user_id=99)


@pytest.fixture(autouse=True)
def _reset_pin():
    clear_pinned_brand(_CHAT)
    yield
    clear_pinned_brand(_CHAT)


@pytest.mark.parametrize(
    "text",
    [
        "이니어 후드집업처럼 얇고 가벼운 후드집업을 찾고있어 룰루레몬이나 유니클로 처럼",
        "이니어 같은 후드집업",
        "유니클로 느낌 셔츠",
    ],
)
def test_comparison_brand_is_not_pinned(monkeypatch, text):
    _seed(monkeypatch)
    set_pinned_brand(_CHAT, ["Uniqlo"])  # 이전 턴의 핀도 지운다(새 화제)
    _build_ctx(_state(text), _Sess())
    assert get_pinned_brand(_CHAT) is None


def test_plain_brand_request_still_pinned(monkeypatch):
    _seed(monkeypatch)
    _build_ctx(_state("이니어 후드집업 보여줘"), _Sess())
    assert get_pinned_brand(_CHAT) == ["innir"]


@pytest.mark.asyncio
async def test_similar_path_ignores_stale_brand_pin(monkeypatch):
    captured: dict[str, object] = {}
    real_build = SearchRepository.build_params
    real_build_h = SearchRepository.build_params_hybrid

    def spy(**kwargs):
        captured["brand_filter"] = kwargs.get("brand_filter")
        return real_build(**kwargs)

    def spy_h(**kwargs):
        captured["brand_filter"] = kwargs.get("brand_filter")
        return real_build_h(**kwargs)

    async def fake_embed_text(q: str):
        return [0.1] * 768

    async def fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates or [])
        return state

    async def fake_rpc(fn_name, params):
        return [{"id": "p1", "name": "Zip Hoodie", "brand": "Other", "distance": 0.1, "degraded": False}]

    async def fake_centroid(names, **_k):
        return [0.2] * 768

    _seed(monkeypatch)
    monkeypatch.setattr(SearchRepository, "build_params", staticmethod(spy))
    monkeypatch.setattr(SearchRepository, "build_params_hybrid", staticmethod(spy_h))
    monkeypatch.setattr("app.pipeline.embed.EmbedProvider.embed_text", staticmethod(fake_embed_text))
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", fake_diversify_step)
    monkeypatch.setattr("app.pipeline.search.DatabaseProvider.rpc", staticmethod(fake_rpc))
    monkeypatch.setattr(DatabaseProvider, "get_brand_centroid_embedding", staticmethod(fake_centroid))

    set_pinned_brand(_CHAT, ["innir"])
    ctx = _build_ctx(_state("hoodie"), _Sess())
    set_pinned_brand(_CHAT, ["innir"])  # 핀이 남아 있는 최악의 경우를 강제
    res = await sp.dispatch({"text_query": "zip hoodie men", "similar_to_brand": "innir"}, ctx)

    assert res["ok"]
    assert captured["brand_filter"] is None


# ── brand-similar 결정론 경로: 품목은 싣고, 서술이 있으면 LLM 에 맡긴다 ──────────


def _msg_state(text: str):
    return SimpleNamespace(image_url=None, message=SimpleNamespace(text=text, callback_data=None))


@pytest.mark.parametrize(
    ("text", "garment", "has_desc"),
    [
        ("이니어 같은 후드집업", "후드집업", False),
        ("유니클로 느낌 셔츠", "셔츠", False),
        ("이니어 같은 옷들", None, False),
        ("이니어 후드집업처럼 얇고 가벼운 후드집업을 찾고있어 룰루레몬이나 유니클로 처럼", "후드집업", True),
    ],
)
def test_brand_similar_parts(monkeypatch, text, garment, has_desc):
    _seed(monkeypatch)
    g, desc = react_loop._brand_similar_parts(text)
    assert g == garment
    assert bool(desc) is has_desc


def test_descriptive_request_goes_to_llm_loop(monkeypatch):
    _seed(monkeypatch)
    long_msg = "이니어 후드집업처럼 얇고 가벼운 후드집업을 찾고있어 룰루레몬이나 유니클로 처럼"
    assert react_loop._detect_brand_similar_intent(_msg_state(long_msg), None) is None
    assert react_loop._detect_brand_similar_intent(_msg_state("이니어 같은 후드집업"), None) == "innir"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "category", "query"),
    [("이니어 같은 후드집업", "tops", "zip-up hoodie"), ("이니어 같은 옷", None, "clothes")],
)
async def test_brand_similar_shortcircuit_carries_garment(monkeypatch, text, category, query):
    _seed(monkeypatch)
    seen: dict = {}

    async def fake_search(args, ctx):
        seen.update(args)
        return {"ok": True, "candidates_count": 10}

    async def fake_respond(args, ctx):
        return {"ok": True}

    monkeypatch.setattr(sp, "dispatch", fake_search)
    monkeypatch.setattr(respond_mod, "dispatch", fake_respond)
    await react_loop._run_brand_similar_shortcircuit("innir", _msg_state(text), None, {"lang": "ko"})
    assert seen["similar_to_brand"] == "innir"
    assert seen.get("category") == category
    assert seen["text_query"] == query
