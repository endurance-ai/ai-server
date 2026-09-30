"""앞서 고정한 상품(모바일 칩)을 칩 없는 지시어 턴이 이어받는다 (2026-09-28).

9월 실유저: "[#646333 · Stüssy · …] 해당 제품에 대해 설명해줘" → "더 비슷하게" → "위에 제품"
에서 봇이 "이전에 검색한 결과가 없어서"라고 답함. 칩은 그 턴에만 붙어 다음 턴 툴 앵커가 사라졌다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

import app.agents.tools.search_products as sp
from app.agents import last_query, react_loop
from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store

_CHAT = 880011
_CHIP = "[#646333 · Stüssy · THOR STORAGE BIN 53L · ₩135,850]"


def _state(text: str) -> WorkingState:
    cm = ChannelMessage(chat_id=_CHAT, from_user_id=_CHAT, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=cm, chat_id=_CHAT, from_user_id=_CHAT)


@pytest.fixture(autouse=True)
def _clean():
    last_query.clear_last_anchor(_CHAT)
    yield
    last_query.clear_last_anchor(_CHAT)


def _ctx_text(text: str) -> str:
    return react_loop._build_ctx(_state(text), get_store().get_or_create(_CHAT))["text_query"]


@pytest.mark.parametrize("followup", ["더 비슷하게", "위에 제품", "이거 더 싸게", "그 제품 다른 색"])
def test_followup_reuses_pinned_chip(followup):
    _ctx_text(f"{_CHIP} 해당 제품에 대해 설명해줘")  # 칩 턴 → 기억
    ctx_text = _ctx_text(followup)
    assert ctx_text.startswith("[#646333")
    assert sp._PINNED_PID_RE.search(ctx_text).group(1) == "646333"
    msg = react_loop._build_user_message(_state(followup), get_store().get_or_create(_CHAT))
    assert "[ANCHOR PRODUCT" in msg and "#646333" in msg


def test_unrelated_message_does_not_get_chip():
    _ctx_text(f"{_CHIP} 더 저렴하게")
    assert _ctx_text("여름 원피스 추천해줘") == "여름 원피스 추천해줘"
    assert _ctx_text("검정 롱코트 중에 울 소재로 가격대 30만원 이하인 거 비슷하게 찾아줘").startswith("검정")


def test_brand_mention_is_not_anchor_followup(monkeypatch):
    from app.infrastructure.repositories import brand_node_cache

    monkeypatch.setattr(brand_node_cache, "scan_text_for_brand", lambda text: ["ZARA"] if "자라" in text else [])
    _ctx_text(f"{_CHIP} 더 저렴하게")
    assert _ctx_text("자라 비슷한 거") == "자라 비슷한 거"
    assert _ctx_text("더 비슷하게").startswith("[#646333")


def test_anchor_expires(monkeypatch):
    _ctx_text(f"{_CHIP} 더 저렴하게")
    monkeypatch.setattr(last_query, "_ANCHOR_TTL_S", -1.0)
    assert _ctx_text("더 비슷하게") == "더 비슷하게"


@pytest.mark.asyncio
async def test_new_search_without_chip_clears_anchor(monkeypatch):
    last_query.set_last_anchor(_CHAT, _CHIP)

    async def fake_search(**_kw):
        return []

    monkeypatch.setattr(sp, "run_text_only_search", fake_search)
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda _ctx: "unisex")
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    await sp.dispatch({"text_query": "black coat"}, {"chat_id": _CHAT, "user_key": "u:1", "text_query": "검정 코트"})
    assert last_query.get_last_anchor(_CHAT) is None
