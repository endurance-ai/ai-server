"""SPEC-AGENT-UX-P0-001 v0.2.0 / REQ-UX-004 — tool dispatch pre-message firing.

검증:
- search_products / refine_search dispatch → "search" pre-message 를 **발사하지
  않는다** (2026-08-26 제거: 스피너가 이미 검색 중임을 보여줘 잉여).
- analyze_image dispatch → PRE_MESSAGES["analyze_image"][lang] 1회.
- KO/EN 둘 다 ctx["lang"] 따라 분기.
- send_text 가 raise 해도 본 작업 (vision) 은 그대로 실행.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.channels.adapter import MessengerAdapter
from app.channels.pre_messages import PRE_MESSAGES, _reset_node_markers_for_tests
from app.channels.schemas import BotCard, ChannelMessage


class _SpyAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []

    async def parse_inbound(self, payload: dict) -> ChannelMessage:  # pragma: no cover
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:  # pragma: no cover
        return None


class _RaisingTextAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.send_text_called = 0

    async def parse_inbound(self, payload: dict) -> ChannelMessage:  # pragma: no cover
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        self.send_text_called += 1
        raise RuntimeError("simulated send failure")

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _reset_markers() -> None:
    _reset_node_markers_for_tests()


def _bind_adapter(monkeypatch: pytest.MonkeyPatch, adapter: MessengerAdapter) -> None:
    from app.graphs.nodes import _adapter_ctx

    _adapter_ctx.set_adapter(adapter)


@pytest.fixture(autouse=True)
def _pin_gender(monkeypatch: pytest.MonkeyPatch) -> None:
    """260522 (SPEC-GENDER-PIN-001): these tests assert the pre-message is the
    ONLY text sent on a genderless search. With the gender pin, a genderless
    query + no pinned gender now also fires a one-time gender card (an extra
    send). Pin the profile gender to 'unisex' so the gate passes silently and
    these tests keep measuring ONLY pre-message firing."""
    monkeypatch.setattr(
        "app.agents.tools.search_products._lookup_profile_gender",
        lambda ctx: "unisex",
    )


def _make_ctx(lang: str = "ko", chat_id: int = 42, image_url: str | None = None) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "from_user_id": 99,
        "user_key": "u:test",
        "image_url": image_url,
        "thread_id": "test-thread",
        "lang": lang,
        "text_query": "leather loafers",
        "style_node_primary": None,
        "vision_category": None,
    }


# ── search_products / refine_search 는 pre-message 를 발사하지 않는다 ──────
# (2026-08-26 제거: 스피너/typing 이 이미 검색 중임을 보여줘 잉여였다.)


@pytest.mark.asyncio
async def test_search_products_dispatch_sends_no_pre_message(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)
    # search_step / diversify 경로 우회 — run_text_only_search 스텁.
    monkeypatch.setattr(
        "app.agents.tools.search_products.run_text_only_search",
        AsyncMock(return_value=[]),
    )

    from app.agents.tools.search_products import dispatch

    ctx = _make_ctx(lang="ko")
    await dispatch({"text_query": "leather loafers"}, ctx)
    assert spy.texts == []


@pytest.mark.asyncio
async def test_refine_search_dispatch_sends_no_pre_message(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)
    monkeypatch.setattr(
        "app.agents.tools.refine_search.run_text_only_search",
        AsyncMock(return_value=[]),
    )

    from app.agents.tools.refine_search import dispatch as refine_dispatch

    await refine_dispatch({"action": "broaden"}, _make_ctx(lang="ko"))
    assert spy.texts == []


# ── analyze_image fires its own pre-message ───────────────────────────────


@pytest.mark.asyncio
async def test_analyze_image_dispatch_fires_analyze_image_message_en(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)
    # vision_extract 우회.
    fake_result = type("V", (), {"styleNode": None, "mood": [], "palette": [], "items": []})()
    monkeypatch.setattr(
        "app.channels.vision.extract",
        AsyncMock(return_value=fake_result),
    )

    from app.agents.tools.analyze_image import dispatch as analyze_dispatch

    await analyze_dispatch(
        {},
        _make_ctx(lang="en", image_url="https://cdn.example.com/x.jpg"),
    )
    assert spy.texts == [(42, PRE_MESSAGES["analyze_image"]["en"])]


@pytest.mark.asyncio
async def test_analyze_image_dispatch_ko_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)
    fake_result = type("V", (), {"styleNode": None, "mood": [], "palette": [], "items": []})()
    monkeypatch.setattr(
        "app.channels.vision.extract",
        AsyncMock(return_value=fake_result),
    )

    from app.agents.tools.analyze_image import dispatch as analyze_dispatch

    await analyze_dispatch(
        {},
        _make_ctx(lang="ko", image_url="https://cdn.example.com/x.jpg"),
    )
    assert spy.texts == [(42, PRE_MESSAGES["analyze_image"]["ko"])]


@pytest.mark.asyncio
async def test_analyze_image_skips_pre_message_when_missing_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """missing image → early-return 으로 pre-message 도 건너뛴다."""
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)

    from app.agents.tools.analyze_image import dispatch as analyze_dispatch

    result = await analyze_dispatch({}, _make_ctx(image_url=None))
    assert result["ok"] is False
    assert spy.texts == []


# ── Fail-open: send_text raise → tool body still runs (analyze_image) ─────


@pytest.mark.asyncio
async def test_analyze_image_send_text_failure_does_not_block_vision(monkeypatch: pytest.MonkeyPatch) -> None:
    raising = _RaisingTextAdapter()
    _bind_adapter(monkeypatch, raising)
    fake_result = type("V", (), {"styleNode": None, "mood": [], "palette": [], "items": []})()
    vision_stub = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("app.channels.vision.extract", vision_stub)

    from app.agents.tools.analyze_image import dispatch as analyze_dispatch

    result = await analyze_dispatch(
        {},
        _make_ctx(lang="ko", image_url="https://cdn.example.com/x.jpg"),
    )
    assert raising.send_text_called == 1
    assert vision_stub.await_count == 1
    assert result["ok"] is True
