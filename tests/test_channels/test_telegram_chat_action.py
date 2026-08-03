"""SPEC-AGENT-UX-P0-001 / REQ-UX-003 — Telegram sendChatAction unit tests.

- ABC `MessengerAdapter.send_chat_action` 의 default 는 `return False` (no-op).
- `TelegramAdapter.send_chat_action` 은 POST sendChatAction, fail-open swallow,
  `bool` 반환 (성공 True / 실패 False).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from app.channels.adapter import MessengerAdapter
from app.channels.schemas import BotCard, ChannelMessage
from app.channels.telegram.adapter import TelegramAdapter


class _DefaultAdapter(MessengerAdapter):
    """ABC default 만 사용하는 fake (`send_chat_action` override 없음)."""

    async def parse_inbound(self, payload: dict) -> ChannelMessage:  # pragma: no cover
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:  # pragma: no cover
        return None

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:  # pragma: no cover
        return None


def _telegram_with_transport(handler) -> TelegramAdapter:  # noqa: ANN001
    """Wire a TelegramAdapter to an httpx MockTransport."""
    adapter = TelegramAdapter(bot_token="TESTTOKEN")
    transport = httpx.MockTransport(handler)
    adapter._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0, connect=3.0))
    return adapter


# ── ABC default ──────────────────────────────────────────────────────────


async def test_abc_default_returns_false() -> None:
    fa = _DefaultAdapter()
    result = await fa.send_chat_action(42)
    assert result is False


async def test_abc_default_signature_is_bool_typing() -> None:
    """default `action` 은 'typing'."""
    fa = _DefaultAdapter()
    # 인자 없이 호출 가능 (default action='typing').
    assert (await fa.send_chat_action(1)) is False


# ── Telegram impl: success path ──────────────────────────────────────────


async def test_send_chat_action_posts_to_telegram() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    adapter = _telegram_with_transport(handler)
    try:
        ok = await adapter.send_chat_action(chat_id=42)
    finally:
        await adapter.aclose()

    assert ok is True
    assert captured["url"].endswith("/botTESTTOKEN/sendChatAction")
    assert captured["body"] == {"chat_id": 42, "action": "typing"}


async def test_send_chat_action_custom_action() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": True})

    adapter = _telegram_with_transport(handler)
    try:
        ok = await adapter.send_chat_action(chat_id=42, action="upload_photo")
    finally:
        await adapter.aclose()
    assert ok is True
    assert captured["body"]["action"] == "upload_photo"


# ── Fail-open ────────────────────────────────────────────────────────────


async def test_send_chat_action_fail_open_on_http_500(caplog: pytest.LogCaptureFixture) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"ok": False})

    adapter = _telegram_with_transport(handler)
    try:
        with caplog.at_level(logging.DEBUG, logger="app.channels.telegram.adapter"):
            ok = await adapter.send_chat_action(42)
    finally:
        await adapter.aclose()
    assert ok is False
    # 어떤 WARN/ERROR 도 새로 발생하지 않아야 함 (fail-open).
    levels = {r.levelno for r in caplog.records if r.name == "app.channels.telegram.adapter"}
    assert logging.ERROR not in levels


async def test_send_chat_action_fail_open_on_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timeout")

    adapter = _telegram_with_transport(handler)
    try:
        ok = await adapter.send_chat_action(42)
    finally:
        await adapter.aclose()
    assert ok is False


async def test_send_chat_action_fail_open_on_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = _telegram_with_transport(handler)
    try:
        ok = await adapter.send_chat_action(42)
    finally:
        await adapter.aclose()
    assert ok is False


# ── No new httpx client constructed inside send_chat_action ──────────────


async def test_send_chat_action_reuses_existing_client() -> None:
    """기존 `self._client` 재사용 — 별도 `httpx.AsyncClient` 인스턴스화 금지."""
    import inspect

    src = inspect.getsource(TelegramAdapter.send_chat_action)
    assert "httpx.AsyncClient(" not in src
