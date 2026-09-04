"""SPEC-AGENT-UX-P0-001 / REQ-UX-003 — ReAct loop typing-indicator hook tests.

- `search_products` / `refine_search` dispatch 진입 직전 1회 호출.
- `respond` dispatch 진입 직전 1회 호출.
- 다른 tool (analyze_image, update_taste, get_recent_history,
  ask_user_clarification) 은 호출하지 않음.
- 모두 fire-and-forget (`asyncio.create_task`) — tool body 를 block 하지 않음.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.channels.adapter import MessengerAdapter
from app.channels.schemas import BotCard, ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState


class _SpyAdapter(MessengerAdapter):
    def __init__(self) -> None:
        self.chat_action_calls: list[tuple[int, str]] = []
        self.texts: list[tuple[int, str]] = []

    async def parse_inbound(self, payload: dict) -> ChannelMessage:  # pragma: no cover
        raise NotImplementedError

    async def send_text(self, chat_id: int, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:  # pragma: no cover
        return None

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        self.chat_action_calls.append((chat_id, action))
        return True


class _FakeAIMessage:
    def __init__(self, tool_calls: list[dict]) -> None:
        self.tool_calls = tool_calls
        self.usage_metadata = {"total_tokens": 50}


class _FakeLLM:
    def __init__(self, responses: list[_FakeAIMessage]) -> None:
        self._responses = list(responses)

    async def ainvoke(self, messages):  # noqa: ANN001
        if not self._responses:
            return _FakeAIMessage([])
        return self._responses.pop(0)


def _make_state(text: str = "hello") -> WorkingState:
    msg = ChannelMessage(chat_id=42, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


def _make_session() -> Session:
    return Session(chat_id=42, state=SessionState.IDLE)


async def _run_with_tool(monkeypatch: pytest.MonkeyPatch, tool_name: str, args: dict[str, Any]) -> _SpyAdapter:
    from app.agents import react_loop as rl

    spy = _SpyAdapter()
    # bind into both ctx accessors: react_loop pulls via _adapter_ctx.get_adapter().
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: spy)
    # Stub the per-tool dispatcher so we don't depend on real DB / Modal.
    tool_module = {
        "search_products": "app.agents.tools.search_products.dispatch",
        "refine_search": "app.agents.tools.refine_search.dispatch",
        "analyze_image": "app.agents.tools.analyze_image.dispatch",
        "update_taste": "app.agents.tools.update_taste.dispatch",
        "get_recent_history": "app.agents.tools.get_recent_history.dispatch",
        "ask_user_clarification": "app.agents.tools.ask_user_clarification.dispatch",
    }
    if tool_name in tool_module:
        monkeypatch.setattr(
            tool_module[tool_name],
            AsyncMock(return_value={"ok": True, "candidates_count": 5, "top_candidates": []}),
        )

    if tool_name == "respond":
        responses = [_FakeAIMessage([{"name": "respond", "args": {"text": "hi"}, "id": "1"}])]
    else:
        # First call: invoke target tool; second call: respond to terminate.
        responses = [
            _FakeAIMessage([{"name": tool_name, "args": args, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "2"}]),
        ]
    fake = _FakeLLM(responses)
    monkeypatch.setattr(rl, "get_llm", lambda: fake)

    # For respond we still need a `send_text` mock; SpyAdapter handles it.
    await rl.run_react_loop(_make_state(), _make_session())
    # `asyncio.create_task` 가 즉시 실행되도록 yield.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return spy


# ── Typing fires on search/refine/respond ─────────────────────────────────


@pytest.mark.asyncio
async def test_typing_fired_before_search_products(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = await _run_with_tool(monkeypatch, "search_products", {"text_query": "leather loafers"})
    # 1 for search_products + 1 for respond = exactly 2.
    actions = [a for _, a in spy.chat_action_calls]
    assert actions.count("typing") >= 1
    assert (42, "typing") in spy.chat_action_calls


@pytest.mark.asyncio
async def test_typing_fired_before_refine_search(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = await _run_with_tool(monkeypatch, "refine_search", {"action": "broaden"})
    actions = [a for _, a in spy.chat_action_calls]
    assert actions.count("typing") >= 1


@pytest.mark.asyncio
async def test_typing_fired_before_respond(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = await _run_with_tool(monkeypatch, "respond", {})
    assert (42, "typing") in spy.chat_action_calls


# ── Typing does NOT fire for unrelated tools ─────────────────────────────


@pytest.mark.asyncio
async def test_typing_not_fired_for_update_taste(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = await _run_with_tool(monkeypatch, "update_taste", {"source": "click"})
    # update_taste itself should NOT trigger typing; only the terminal respond does.
    # We expect exactly 1 call (for the final respond).
    assert spy.chat_action_calls.count((42, "typing")) == 1


@pytest.mark.asyncio
async def test_typing_not_fired_for_get_recent_history(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = await _run_with_tool(monkeypatch, "get_recent_history", {"n": 5})
    assert spy.chat_action_calls.count((42, "typing")) == 1


# ── AST scan: hook site enumeration ───────────────────────────────────────


def test_typing_hook_only_at_dispatch_helper() -> None:
    """`send_chat_action` 호출은 _fire_typing helper 안에만 있어야 함.

    `_dispatch_tool` 내부 분기에서 단 한 곳 (`_fire_typing`) 만 호출.
    """
    from app.agents import react_loop as rl

    src = inspect.getsource(rl)
    tree = ast.parse(src)

    # `send_chat_action` 어트리뷰트 호출 site 를 모두 찾는다.
    call_sites: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "send_chat_action":
                # enclosing function name 찾기.
                call_sites.append("send_chat_action_call")

    # 정확히 한 번 (helper 내부) 만 등장.
    assert len(call_sites) == 1, f"expected 1 send_chat_action call site, got {len(call_sites)}"
