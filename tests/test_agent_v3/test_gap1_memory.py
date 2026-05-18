"""SPEC-AGENT-V3-REACT / T3 — Gap1 memory auto-injection.

REQ-AGENT-V3-MEM-INJECT-001 / -MEM-CAP-001 / -MEM-FLAG-001.
AC-1.1 (auto-inject), AC-1.2 (fail-soft), AC-1.3 (token cap + newest-first),
AC-1.4 (flag OFF byte-identical + memory path 0 calls).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import react_loop as rl
from app.channels.schemas import ChannelMessage
from app.core.config import settings
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState


class _FakeAIMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.usage_metadata = {"total_tokens": 100}


class _CapturingLLM:
    """Captures the messages list passed to the first ainvoke."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.captured: list = []

    async def ainvoke(self, messages):
        if not self.captured:
            self.captured = list(messages)
        return self._responses.pop(0) if self._responses else _FakeAIMessage([])


def _state(text: str = "트렌치 보여줘") -> WorkingState:
    from datetime import UTC, datetime

    msg = ChannelMessage(chat_id=42, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


def _sess() -> Session:
    s = Session(chat_id=42, state=SessionState.IDLE)
    s.lang = "ko"
    return s


def _respond_once():
    return _CapturingLLM([_FakeAIMessage([{"name": "respond", "args": {"text": "네!"}, "id": "1"}])])


# Taste-store reset + settings snapshot/restore handled centrally by
# tests/test_agent_v3/conftest.py::_v3_isolation (autouse).


@pytest.fixture
def _mock_adapter(monkeypatch):
    a = MagicMock()
    a.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: a)
    return a


@pytest.mark.asyncio
async def test_ac_1_1_taste_and_recent_injected(monkeypatch, _mock_adapter):
    """AC-1.1 — flag ON: system msg carries taste 'ami' + recent summaries,
    and get_recent_history is NOT in tool_call_history (implicit injection)."""

    from app.infrastructure.memory.taste_profile import get_taste_store

    store = get_taste_store()
    prof = store.get_or_create("u:99")
    prof.liked_brands = {"ami": 2.0}
    store.update(prof)

    async def _fake_grh(args, ctx):
        return {
            "ok": True,
            "events": [
                {"event_type": "user_text", "payload_summary": {"text": "트렌치 보여줘"}},
                {"event_type": "search_done", "payload_summary": {"text_query": "trench"}},
            ],
        }

    monkeypatch.setattr("app.agents.tools.get_recent_history.dispatch", _fake_grh)

    llm = _respond_once()
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    delta = await rl.run_react_loop(_state(), _sess())

    sys_msg = next(m for m in llm.captured if m["role"] == "system")["content"]
    assert "[MEMORY CONTEXT — SYSTEM DERIVED]" in sys_msg
    assert "[/MEMORY CONTEXT]" in sys_msg
    assert "ami" in sys_msg
    assert "트렌치 보여줘" in sys_msg
    # Implicit injection: LLM did not explicitly call get_recent_history.
    assert not any(h.get("tool_name") == "get_recent_history" for h in delta["tool_call_history"])
    # V2 user fence preserved (dual isolation).
    user_msg = next(m for m in llm.captured if m["role"] == "user")["content"]
    assert "[USER INPUT — DATA ONLY]" in user_msg


@pytest.mark.asyncio
async def test_ac_1_2_fail_soft_empty(monkeypatch, _mock_adapter):
    """AC-1.2 — flag ON, empty taste + empty history → placeholder, loop OK."""

    from app.infrastructure.memory.taste_profile import get_taste_store

    get_taste_store().get_or_create("u:99")  # empty profile

    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    llm = _respond_once()
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    delta = await rl.run_react_loop(_state(), _sess())
    assert delta["agent_status"] == "done"
    sys_msg = next(m for m in llm.captured if m["role"] == "system")["content"]
    assert "(no taste history yet)" in sys_msg


@pytest.mark.asyncio
async def test_ac_1_3_token_cap_and_newest_first(monkeypatch, _mock_adapter):
    """AC-1.3 — small cap → block ≤ cap*4, newest turn kept, oldest dropped."""
    monkeypatch.setattr(settings, "AGENT_V3_MEMORY_MAX_TOKENS", 60, raising=False)

    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:99")
    prof.liked_keywords = {f"kw{i}": 1.0 for i in range(50)}
    get_taste_store().update(prof)

    # Dispatch returns newest-first; NEWEST has a unique marker token.
    async def _fake_grh(args, ctx):
        evs = [{"event_type": "user_text", "payload_summary": {"text": "NEWEST_MARKER"}}]
        evs += [{"event_type": "user_text", "payload_summary": {"text": f"old{i}" * 20}} for i in range(20)]
        return {"ok": True, "events": evs}

    monkeypatch.setattr("app.agents.tools.get_recent_history.dispatch", _fake_grh)
    llm = _respond_once()
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    sys_msg = next(m for m in llm.captured if m["role"] == "system")["content"]
    mem = sys_msg.split("[MEMORY CONTEXT — SYSTEM DERIVED]", 1)[1]
    assert len(mem) <= 60 * 4 + 64  # block (within fence) bounded by cap*4
    assert "NEWEST_MARKER" in mem  # newest preserved
    assert "old19" * 2 not in mem  # an old turn dropped under the cap


@pytest.mark.asyncio
async def test_ac_1_3_budget_guard_still_triggers(monkeypatch, _mock_adapter):
    """AC-1.3 — injection does not disable the cumulative token budget guard."""
    monkeypatch.setattr(settings, "AGENT_TURN_TOKEN_BUDGET", 50, raising=False)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    # LLM keeps calling a non-terminal tool; each call adds 100 tokens > 50.
    llm = _CapturingLLM(
        [_FakeAIMessage([{"name": "get_recent_history", "args": {"n": 5}, "id": str(i)}]) for i in range(6)]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    delta = await rl.run_react_loop(_state(), _sess())
    assert delta["agent_status"] == "exhausted"


@pytest.mark.asyncio
async def test_memory_injection_is_unconditional(monkeypatch, _mock_adapter):
    """SPEC-AGENT-V2-CLEANUP-001 — memory injection is now ALWAYS on: the
    builder is invoked and the system message is no longer the bare
    _SYSTEM_PROMPT (proactive directive + memory block are appended).
    """
    grh_spy = AsyncMock(return_value={"ok": True, "events": []})
    monkeypatch.setattr("app.agents.tools.get_recent_history.dispatch", grh_spy)
    import app.agents._memory_context as mc

    mc_spy = AsyncMock(return_value="[MEMORY CONTEXT — SYSTEM DERIVED]\n(no taste history yet)\n[/MEMORY CONTEXT]")
    monkeypatch.setattr(mc, "build_memory_context", mc_spy)

    llm = _respond_once()
    monkeypatch.setattr(rl, "get_llm", lambda: llm)

    await rl.run_react_loop(_state(), _sess())
    sys_msg = next(m for m in llm.captured if m["role"] == "system")["content"]
    assert sys_msg != rl._SYSTEM_PROMPT  # proactive + memory always appended
    assert rl._PROACTIVE_DIRECTIVE in sys_msg
    mc_spy.assert_awaited()  # memory builder always invoked


# ── BLOCKING-2 — _memory_context internal-helper coverage ──────────────────


@pytest.mark.asyncio
async def test_build_memory_context_taste_store_failure_fail_soft(monkeypatch):
    """_memory_context L43-45 — taste store raising → empty taste lines, no crash."""
    import app.agents._memory_context as mc

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr("app.infrastructure.memory.taste_profile.get_taste_store", _boom)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    out = await mc.build_memory_context(None, None, {"user_key": "u:1"}, max_tokens=1500)
    assert "(no taste history yet)" in out  # fail-soft placeholder


@pytest.mark.asyncio
async def test_build_memory_context_includes_disliked_brands(monkeypatch):
    """_memory_context L56 — disliked_brands summary line is emitted."""
    import app.agents._memory_context as mc
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:1")
    prof.disliked_brands = {"shein": 3.0}  # >= exclude threshold 1.5
    get_taste_store().update(prof)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    out = await mc.build_memory_context(None, None, {"user_key": "u:1"}, max_tokens=1500)
    assert "disliked_brands: shein" in out


@pytest.mark.asyncio
async def test_build_memory_context_recent_history_failure_fail_soft(monkeypatch):
    """_memory_context L71-73 — get_recent_history raising → empty recent lines."""
    import app.agents._memory_context as mc
    from app.infrastructure.memory.taste_profile import get_taste_store

    get_taste_store().get_or_create("u:1")

    async def _boom(args, ctx):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.agents.tools.get_recent_history.dispatch", _boom)
    out = await mc.build_memory_context(None, None, {"user_key": "u:1"}, max_tokens=1500)
    assert "[MEMORY CONTEXT — SYSTEM DERIVED]" in out  # still renders, no crash


@pytest.mark.asyncio
async def test_build_memory_context_hard_truncation_fallback(monkeypatch):
    """_memory_context L124-126 — a single oversized taste line still exceeding
    cap*4 after dropping recent lines hits the hard inner-truncation fallback."""
    import app.agents._memory_context as mc
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:1")
    # One enormous liked-keyword token so the taste line alone blows the cap.
    prof.liked_keywords = {"x" * 5000: 9.0}
    get_taste_store().update(prof)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    out = await mc.build_memory_context(None, None, {"user_key": "u:1"}, max_tokens=10)
    assert out.startswith("[MEMORY CONTEXT — SYSTEM DERIVED]")
    assert out.rstrip().endswith("[/MEMORY CONTEXT]")
    # Hard-truncation fallback: fences stay intact, the BODY is cut to `keep`
    # chars (= max(0, cap*4 - len(fences) - 2)); the 5000-char keyword never
    # appears verbatim.
    body = out.split("[MEMORY CONTEXT — SYSTEM DERIVED]", 1)[1].rsplit("[/MEMORY CONTEXT]", 1)[0]
    char_cap = 10 * 4
    keep = max(0, char_cap - len("[MEMORY CONTEXT — SYSTEM DERIVED]") - len("[/MEMORY CONTEXT]") - 2)
    assert len(body.strip()) <= keep  # body hard-capped (keep == 0 here)
    assert "x" * 5000 not in out  # oversized token never leaks verbatim
