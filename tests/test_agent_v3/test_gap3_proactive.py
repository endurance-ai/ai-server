"""SPEC-AGENT-V3-REACT / T8 — Gap3 proactive suggestion.

REQ-AGENT-V3-PROACT-TOOL-001 / -PROACT-PROMPT-001 / -PROACT-FLAG-001.
AC-3.1 (8th tool register + send), AC-3.2 (directive in prompt),
AC-3.3 (flag OFF 7-tool + prompt byte-identical).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import react_loop as rl
from app.agents import tool_registry as tr
from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState


class _FakeAIMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.usage_metadata = {"total_tokens": 10}


class _CapturingLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.captured: list = []

    async def ainvoke(self, messages):
        if not self.captured:
            self.captured = list(messages)
        return self._responses.pop(0) if self._responses else _FakeAIMessage([])


def _state() -> WorkingState:
    from datetime import UTC, datetime

    msg = ChannelMessage(chat_id=42, text="옷 추천", received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


def _sess() -> Session:
    s = Session(chat_id=42, state=SessionState.IDLE)
    s.lang = "ko"
    return s


@pytest.fixture
def _adapter(monkeypatch):
    a = MagicMock()
    a.send_text = AsyncMock()
    a.send_text_with_buttons = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: a)
    return a


@pytest.fixture
def _proactive_on():
    """SPEC-AGENT-V2-CLEANUP-001 — proactive (8th tool + directive) is now
    unconditional; this fixture is a no-op kept for test readability."""
    yield


def test_ac_3_1_registry_eight_tool(_proactive_on):
    """SPEC-AGENT-V2-CLEANUP-001 — TOOL_NAMES length 8, suggest_next_step
    present, validate_args works (unconditional)."""
    assert len(tr.TOOL_NAMES) == 8
    assert "suggest_next_step" in tr.TOOL_NAMES
    ok, err = tr.validate_args("suggest_next_step", {"kind": "similar", "options": ["a"], "prompt": "p"})
    assert ok and err is None


@pytest.mark.asyncio
async def test_ac_3_1_dispatch_sends_card_no_terminate(_proactive_on, _adapter, monkeypatch):
    """AC-3.1 — LLM calls suggest_next_step → adapter send, loop NOT terminated."""
    llm = _CapturingLLM(
        [
            _FakeAIMessage(
                [
                    {
                        "name": "suggest_next_step",
                        "args": {
                            "kind": "different_mood",
                            "options": ["더 캐주얼", "다른 무드"],
                            "prompt": "어떤 게 좋아요?",
                        },
                        "id": "1",
                    }
                ]
            ),
            _FakeAIMessage([{"name": "respond", "args": {"text": "골라보세요!"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)
    delta = await rl.run_react_loop(_state(), _sess())
    assert delta["agent_status"] == "done"
    assert delta["agent_iterations"] == 2  # suggest did NOT terminate
    _adapter.send_text_with_buttons.assert_awaited()


@pytest.mark.asyncio
async def test_ac_3_2_directive_in_system_prompt(_proactive_on, _adapter, monkeypatch):
    """AC-3.2 — flag ON: system message carries the proactive directive."""
    llm = _CapturingLLM([_FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "1"}])])
    monkeypatch.setattr(rl, "get_llm", lambda: llm)
    await rl.run_react_loop(_state(), _sess())
    raw = next(m for m in llm.captured if m["role"] == "system")["content"]
    sys_msg = raw[0]["text"] if isinstance(raw, list) else raw
    assert "Be proactive" in sys_msg
    assert "candidates_count < 3" in sys_msg
    assert "suggest_next_step" in sys_msg
    # Original V2 prompt still present (additive).
    assert "You are kiko" in sys_msg


def test_registry_is_unconditionally_eight_tool():
    """SPEC-AGENT-V2-CLEANUP-001 — the 8-tool registry is permanent;
    suggest_next_step is always the 8th tool."""
    assert tr.TOOL_NAMES == (
        "analyze_image",
        "search_products",
        "refine_search",
        "update_taste",
        "ask_user_clarification",
        "get_recent_history",
        "respond",
        "suggest_next_step",
    )
    assert not hasattr(tr, "_rebuild_registry_for_flag")


# ── BLOCKING-2 — suggest_next_step.dispatch error-path coverage ────────────


@pytest.mark.asyncio
async def test_dispatch_invalid_kind():
    """L27-28 — unknown `kind` → invalid_kind, no send."""
    from app.agents.tools.suggest_next_step import dispatch

    r = await dispatch({"kind": "bogus", "options": ["a"], "prompt": "p"}, {"chat_id": 1})
    assert r["ok"] is False
    assert r["error"] == "invalid_kind:bogus"
    assert r["card_sent"] is False
    assert r["kind"] == "bogus"


@pytest.mark.asyncio
async def test_dispatch_missing_options_or_prompt():
    """L32-33 — empty options OR empty prompt → missing_options_or_prompt."""
    from app.agents.tools.suggest_next_step import dispatch

    r1 = await dispatch({"kind": "similar", "options": [], "prompt": "p"}, {"chat_id": 1})
    assert r1["error"] == "missing_options_or_prompt" and r1["ok"] is False
    r2 = await dispatch({"kind": "similar", "options": ["a"], "prompt": "   "}, {"chat_id": 1})
    assert r2["error"] == "missing_options_or_prompt" and r2["ok"] is False


@pytest.mark.asyncio
async def test_dispatch_missing_chat_id():
    """L36-37 — ctx without chat_id → missing_chat_id."""
    from app.agents.tools.suggest_next_step import dispatch

    r = await dispatch({"kind": "similar", "options": ["a"], "prompt": "p"}, {})
    assert r["ok"] is False
    assert r["error"] == "missing_chat_id"


@pytest.mark.asyncio
async def test_dispatch_send_text_fallback_when_no_buttons(monkeypatch):
    """L46-47 — adapter without send_text_with_buttons → send_text fallback."""
    from app.agents.tools import suggest_next_step as sns

    class _PlainAdapter:
        def __init__(self):
            self.text_calls = 0

        async def send_text(self, chat_id, text):
            self.text_calls += 1
            return "mid"

    adapter = _PlainAdapter()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)
    r = await sns.dispatch({"kind": "similar", "options": ["a"], "prompt": "p"}, {"chat_id": 9})
    assert r["ok"] is True
    assert r["card_sent"] is True
    assert adapter.text_calls == 1  # fallback path exercised


@pytest.mark.asyncio
async def test_dispatch_send_raises_returns_send_failed(monkeypatch):
    """L48-50 — adapter send raising → send_failed:<ExcType>, card_sent False."""
    from app.agents.tools import suggest_next_step as sns

    class _BoomAdapter:
        async def send_text_with_buttons(self, chat_id, text, buttons):
            raise RuntimeError("telegram down")

    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: _BoomAdapter())
    r = await sns.dispatch({"kind": "fit_change", "options": ["a"], "prompt": "p"}, {"chat_id": 9})
    assert r["ok"] is False
    assert r["error"] == "send_failed:RuntimeError"
    assert r["card_sent"] is False
