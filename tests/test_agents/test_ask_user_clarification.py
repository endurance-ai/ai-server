"""Unit tests for the ask_user_clarification tool wrapper.

SPEC-AGENT-V2-REACT / T-003e. Covers axis validation, the missing
options/prompt and missing chat_id guards, the happy path (card sent via the
bound adapter), and adapter-raise fail-open.
"""

from __future__ import annotations

import pytest

from app.agents.tools import ask_user_clarification as tool
from app.graphs.nodes import _adapter_ctx


class _FakeAdapter:
    """Records send_text_with_buttons calls."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def send_text_with_buttons(self, chat_id, text, buttons):
        self.calls.append((chat_id, text, buttons))


class _TextOnlyAdapter:
    """No send_text_with_buttons → falls back to send_text."""

    def __init__(self):
        self.texts: list[tuple] = []

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))


class _RaisingAdapter:
    async def send_text_with_buttons(self, chat_id, text, buttons):
        raise RuntimeError("boom")


@pytest.fixture
def bind_adapter():
    def _bind(adapter):
        # set_adapter(None) at teardown rather than reset_adapter(token):
        # pytest-asyncio runs the test body in a separate context, so a token
        # captured here cannot be reset from the fixture's own context.
        _adapter_ctx.set_adapter(adapter)
        return adapter

    yield _bind
    _adapter_ctx.set_adapter(None)


# ── validation guards ───────────────────────────────────────────────────────


async def test_invalid_axis_returns_error_without_sending():
    res = await tool.dispatch(
        {"axis": "not_a_real_axis", "options": ["a"], "prompt": "pick"},
        {"chat_id": 42},
    )
    assert res["ok"] is False
    assert res["card_sent"] is False
    assert res["error"].startswith("invalid_axis:")
    assert res["axis"] == "not_a_real_axis"


async def test_missing_options_returns_error():
    res = await tool.dispatch({"axis": "fit", "options": [], "prompt": "pick"}, {"chat_id": 42})
    assert res["ok"] is False
    assert res["error"] == "missing_options_or_prompt"
    assert res["axis"] == "fit"


async def test_missing_prompt_returns_error():
    res = await tool.dispatch({"axis": "fit", "options": ["a"], "prompt": "   "}, {"chat_id": 42})
    assert res["ok"] is False
    assert res["error"] == "missing_options_or_prompt"


async def test_missing_chat_id_returns_error():
    res = await tool.dispatch({"axis": "fit", "options": ["a"], "prompt": "pick"}, {})
    assert res["ok"] is False
    assert res["error"] == "missing_chat_id"


# ── happy path ──────────────────────────────────────────────────────────────


async def test_valid_axis_sends_card(bind_adapter):
    adapter = bind_adapter(_FakeAdapter())
    res = await tool.dispatch(
        {"axis": "fit", "options": ["slim", "relaxed"], "prompt": "어떤 핏?"},
        {"chat_id": 42},
    )
    assert res["ok"] is True
    assert res["card_sent"] is True
    assert res["axis"] == "fit"
    assert len(adapter.calls) == 1
    chat_id, text, buttons = adapter.calls[0]
    assert chat_id == 42
    assert text == "어떤 핏?"
    # callback shape clarify:{axis}:{value}
    assert buttons[0][0]["callback_data"] == "clarify:fit:slim"
    assert buttons[1][0]["callback_data"] == "clarify:fit:relaxed"


async def test_falls_back_to_send_text_when_no_button_method(bind_adapter):
    adapter = bind_adapter(_TextOnlyAdapter())
    res = await tool.dispatch(
        {"axis": "occasion", "options": ["work"], "prompt": "언제 입어요?"},
        {"chat_id": 7},
    )
    assert res["ok"] is True
    assert res["card_sent"] is True
    assert adapter.texts == [(7, "언제 입어요?")]


async def test_options_capped_at_eight(bind_adapter):
    adapter = bind_adapter(_FakeAdapter())
    opts = [f"o{i}" for i in range(20)]
    await tool.dispatch({"axis": "fit", "options": opts, "prompt": "p"}, {"chat_id": 1})
    _, _, buttons = adapter.calls[0]
    assert len(buttons) == 8


# ── adapter raise → fail-open ───────────────────────────────────────────────


async def test_adapter_raise_returns_send_failed(bind_adapter):
    bind_adapter(_RaisingAdapter())
    res = await tool.dispatch(
        {"axis": "fit", "options": ["slim"], "prompt": "p"},
        {"chat_id": 42},
    )
    assert res["ok"] is False
    assert res["card_sent"] is False
    assert res["error"].startswith("send_failed:")
    assert res["axis"] == "fit"
