"""성별 카드 탭 후 재검색이 원래 검색 조건(브랜드·가격 등)을 유지한다 (2026-09-28).

예전엔 카드 전송 시 text_query/category/top_k 만 stash 해, 탭 후 재검색에서 브랜드·가격·
제외 조건이 사라졌다("자라 니트 8만원 이하" → 성별 탭 → 자라 아닌 전 가격대 니트).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.agents.tools.search_products as sp
from app.agents import pending_gender
from app.graphs.nodes import ingest

_CHAT = 777001


@pytest.mark.asyncio
async def test_gender_gate_stashes_full_args_and_ctx(monkeypatch):
    monkeypatch.setattr(sp, "_lookup_profile_gender", lambda _ctx: None)
    monkeypatch.setattr(sp, "_send_gender_card", AsyncMock(return_value=True))
    monkeypatch.setattr("app.channels.pre_messages.fire_pre_message", AsyncMock())
    pending_gender.pop_pending(_CHAT)

    args = {"text_query": "knit sweater", "brand": "ZARA", "category": "knitwear", "max_price": 80000}
    ctx = {"chat_id": _CHAT, "user_key": "u:1", "lang": "ko", "user_msg": "자라 니트 8만원 이하", "unrelated": 1}
    res = await sp.dispatch(dict(args), ctx)

    assert res["error"] == "awaiting_gender"
    pending = pending_gender.pop_pending(_CHAT)
    assert pending["args"]["brand"] == "ZARA"
    assert pending["args"]["max_price"] == 80000
    assert pending["ctx"]["user_msg"] == "자라 니트 8만원 이하"
    assert "unrelated" not in pending["ctx"]


@pytest.mark.asyncio
async def test_gender_pick_reruns_dispatch_with_original_args(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_dispatch(args, ctx):
        captured["args"], captured["ctx"] = args, ctx
        return {"ok": True, "candidates_count": 5}

    monkeypatch.setattr(sp, "dispatch", fake_dispatch)
    monkeypatch.setattr("app.agents.tools.respond.send_hybrid_batch", AsyncMock(return_value=5))
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: object())
    monkeypatch.setattr(ingest, "_send_callback_toast", AsyncMock())
    monkeypatch.setattr(
        "app.infrastructure.memory.taste_profile.get_taste_store",
        lambda: SimpleNamespace(get_or_create=lambda _k: SimpleNamespace(gender=None), update=lambda _p: None),
    )
    pending_gender.set_pending(
        _CHAT,
        {
            "text_query": "knit sweater",
            "category": "knitwear",
            "top_k": 15,
            "args": {"text_query": "knit sweater", "brand": "ZARA", "max_price": 80000},
            "ctx": {"chat_id": _CHAT, "user_key": "u:1", "lang": "ko"},
        },
    )
    state = SimpleNamespace(chat_id=_CHAT, from_user_id=1, req_platform=None)
    crumbs: list[str] = []

    await ingest._handle_gender_pick(state, SimpleNamespace(lang="ko"), "women", crumbs)

    assert captured["args"]["brand"] == "ZARA"
    assert captured["args"]["max_price"] == 80000
    assert captured["ctx"]["req_gender"] == "women"
    assert any("delivered=5" in c for c in crumbs)
