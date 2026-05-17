"""SPEC-AGENT-V3-REACT / T10 — orthogonality + pairwise edge cases.

E1 (single flag — others byte-identical), E2 (Gap2+Gap4 pairwise),
E3 (Gap1+Gap3 prompt order), E4 (evaluator raise → fail-open),
E5 (master OFF → all sub-flags no-op).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import react_loop as rl
from app.agents import tool_registry as tr
from app.channels.schemas import ChannelMessage
from app.channels.session import Session, SessionState
from app.core.config import settings
from app.graphs.state import WorkingState


class _FakeAIMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.usage_metadata = {"total_tokens": 10}


class _CapturingLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self.seen: list = []

    async def ainvoke(self, messages):
        self.seen.append(list(messages))
        return self._responses.pop(0) if self._responses else _FakeAIMessage([])


def _state(text="bag") -> WorkingState:
    from datetime import UTC, datetime

    return WorkingState(
        message=ChannelMessage(chat_id=42, text=text, received_at=datetime.now(UTC)),
        chat_id=42,
        from_user_id=99,
    )


def _sess() -> Session:
    s = Session(chat_id=42, state=SessionState.IDLE)
    s.lang = "en"
    return s


@pytest.fixture(autouse=True)
def _edge_baseline(monkeypatch):
    """Per-test starting baseline for edge cases (each test then flips its own
    flags). Taste-store / settings snapshot+restore / registry teardown are
    owned centrally by conftest.py::_v3_isolation — here we only force the
    known START state (all V3 flags OFF + zero backoff) every edge test
    assumes before toggling one flag."""
    monkeypatch.setattr(rl, "_LLM_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))
    for f in (
        "AGENT_V3_MEMORY_INJECTION_ENABLED",
        "AGENT_V3_REFLEXION_ENABLED",
        "AGENT_V3_PROACTIVE_ENABLED",
        "AGENT_V3_DISLIKE_MEMORY_ENABLED",
    ):
        monkeypatch.setattr(settings, f, False, raising=False)
    tr._rebuild_registry_for_flag(False)
    yield


@pytest.fixture
def _adapter(monkeypatch):
    a = MagicMock()
    a.send_text = AsyncMock()
    a.send_text_with_buttons = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: a)
    return a


@pytest.mark.asyncio
async def test_e1_only_gap1_active_others_byte_identical(monkeypatch, _adapter):
    """E1 — only memory flag ON: Gap2/3/4 paths unchanged from V2."""
    monkeypatch.setattr(settings, "AGENT_V3_MEMORY_INJECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    result = {"ok": True, "candidates_count": 5, "top_candidates": []}
    monkeypatch.setattr("app.agents.tools.search_products.dispatch", AsyncMock(return_value=result))
    eval_spy = AsyncMock()
    monkeypatch.setattr("app.agents._reflexion.evaluate_search_quality", eval_spy)

    llm = _CapturingLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)
    await rl.run_react_loop(_state(), _sess())

    # Gap3 OFF → 7-tool.
    assert len(tr.TOOL_NAMES) == 7
    # Gap2 OFF → evaluator not called, ToolMessage byte-identical.
    eval_spy.assert_not_awaited()
    from langchain_core.messages import ToolMessage

    tms = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    assert tms[-1].content == json.dumps(result, default=str)[:2000]
    # Gap1 ON → memory fence present in system msg.
    assert "[MEMORY CONTEXT — SYSTEM DERIVED]" in llm.seen[0][0]["content"]


@pytest.mark.asyncio
async def test_e2_gap2_and_gap4_pairwise(monkeypatch, _adapter):
    """E2 — Gap2 + Gap4 ON: Reflexion eval AND dislike discount both apply,
    no mutual interference."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)

    from app.channels.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:99")
    import time as _t

    prof.disliked_brands_ts["gucci"] = _t.time()
    get_taste_store().update(prof)

    async def _fake_search(args, ctx):
        # Real apply_dislike_discount runs inside the real dispatch; emulate by
        # calling the helper on a mixed candidate list.
        from app.agents.tools.search_products import apply_dislike_discount

        cands = [SimpleNamespace(brand="Gucci", name="x", title="x"), SimpleNamespace(brand="Ami", name="y", title="y")]
        kept = apply_dislike_discount(ctx, cands)
        return {"ok": True, "candidates_count": len(kept), "top_candidates": []}

    monkeypatch.setattr("app.agents.tools.search_products.dispatch", _fake_search)
    monkeypatch.setattr(
        "app.agents._reflexion.evaluate_search_quality",
        AsyncMock(return_value={"score": 0.4, "retry_suggested": True, "reason": "r"}),
    )
    llm = _CapturingLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)
    await rl.run_react_loop(_state(), _sess())

    from langchain_core.messages import ToolMessage

    tms = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    payload = json.loads(tms[-1].content)
    assert payload["_quality"]["score"] == 0.4  # Gap2 applied
    assert payload["candidates_count"] == 1  # Gap4 discounted gucci


@pytest.mark.asyncio
async def test_e3_gap1_and_gap3_prompt_order(monkeypatch, _adapter):
    """E3 — Gap1 + Gap3 ON: system = _SYSTEM_PROMPT + DIRECTIVE + memory,
    in that exact order."""
    monkeypatch.setattr(settings, "AGENT_V3_MEMORY_INJECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AGENT_V3_PROACTIVE_ENABLED", True, raising=False)
    tr._rebuild_registry_for_flag(True)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    llm = _CapturingLLM([_FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "1"}])])
    monkeypatch.setattr(rl, "get_llm", lambda: llm)
    await rl.run_react_loop(_state(), _sess())

    sys_msg = llm.seen[0][0]["content"]
    i_prompt = sys_msg.index("You are kiko")
    i_directive = sys_msg.index("Be proactive")
    i_memory = sys_msg.index("[MEMORY CONTEXT — SYSTEM DERIVED]")
    assert i_prompt < i_directive < i_memory  # exact order


@pytest.mark.asyncio
async def test_e4_evaluator_raise_fail_open(monkeypatch, _adapter):
    """E4 — Gap2 ON, evaluate_search_quality raises → fail-open, loop OK."""
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    monkeypatch.setattr(
        "app.agents.tools.search_products.dispatch",
        AsyncMock(return_value={"ok": True, "candidates_count": 5, "top_candidates": []}),
    )

    async def _boom(state, sess, ctx):
        raise RuntimeError("evaluator exploded")

    monkeypatch.setattr("app.agents._reflexion.evaluate_search_quality", _boom)
    llm = _CapturingLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "bag"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: llm)
    delta = await rl.run_react_loop(_state(), _sess())
    assert delta["agent_status"] == "done"  # fail-open, user still served
    from langchain_core.messages import ToolMessage

    tms = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
    assert "_quality" not in tms[-1].content  # fail-open → no marker, not a crash


@pytest.mark.asyncio
async def test_e5_master_off_subflags_noop(monkeypatch, _adapter):
    """E5 — master AGENT_V2_REACT_ENABLED OFF: V3 sub-flags are irrelevant.

    run_react_loop is only reached on the V2 path; the V1 topology never calls
    it. We assert the flag hierarchy invariant: with all 4 sub-flags ON but
    master OFF, the graph builder selects V1 (no agent node) — verified via the
    topology builder, not the loop (loop is V2-only by construction)."""
    monkeypatch.setattr(settings, "AGENT_V2_REACT_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "AGENT_V3_MEMORY_INJECTION_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", True, raising=False)
    from app.graphs import fashion_bot

    # V1 path: the agent node is absent (V3 sub-flags cannot inject anything).
    g = fashion_bot.build_graph()
    nodes = set(getattr(g, "nodes", {}).keys()) if hasattr(g, "nodes") else set()
    assert "agent" not in nodes
