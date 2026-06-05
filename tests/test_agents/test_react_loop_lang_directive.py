"""SPEC-AGENT-UX-P0-001 / REQ-UX-002 — ReAct loop LANG directive snapshot.

`run_react_loop` 가 LLM 시스템 프롬프트의 LAST line 으로
`[LANG=<ko|en> — MUST reply in <Korean|English>]` 를 주입해야 한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState


class _FakeAIMessage:
    def __init__(self, tool_calls: list[dict]) -> None:
        self.tool_calls = tool_calls
        self.usage_metadata = {"total_tokens": 100}


class _CapturingLLM:
    """첫 ainvoke 호출의 messages 를 캡처 후 즉시 respond tool 로 종료."""

    def __init__(self) -> None:
        self.captured_messages: list[dict] | None = None

    async def ainvoke(self, messages):  # noqa: ANN001
        if self.captured_messages is None:
            self.captured_messages = list(messages)
        return _FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "1"}])


def _make_state(text: str | None = "hello") -> WorkingState:
    msg = ChannelMessage(chat_id=42, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


def _make_session(lang: str = "en") -> Session:
    sess = Session(chat_id=42, state=SessionState.IDLE)
    sess.lang = lang
    return sess


def _system_content(captured: list[dict]) -> str:
    assert captured, "LLM was not invoked"
    assert captured[0]["role"] == "system"
    content = captured[0]["content"]
    if isinstance(content, list):
        # Anthropic prompt-caching format: [{type: text, text: ..., cache_control: ...}]
        return content[0]["text"]
    return content


async def _run_and_capture(monkeypatch: pytest.MonkeyPatch, lang: str, text: str | None = "hello") -> str:
    from app.agents import react_loop as rl

    fake = _CapturingLLM()
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    await rl.run_react_loop(_make_state(text), _make_session(lang=lang))
    return _system_content(fake.captured_messages or [])


# ── REQ-UX-002 acceptance ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_system_prompt_ko_directive_last_line(monkeypatch: pytest.MonkeyPatch) -> None:
    content = await _run_and_capture(monkeypatch, lang="ko", text="안녕")
    # KO directive: 반말 강제 — last block must mention LANG=ko AND 반말 AND forbid 해요체.
    last_block = content.rsplit("\n\n", 1)[-1]
    assert last_block.startswith("[LANG=ko")
    assert "반말" in last_block
    assert "해요체" in last_block  # forbidden marker must appear so LLM sees the constraint
    assert last_block.endswith("]")


@pytest.mark.asyncio
async def test_system_prompt_en_directive_last_line(monkeypatch: pytest.MonkeyPatch) -> None:
    content = await _run_and_capture(monkeypatch, lang="en", text="hello")
    # EN directive: LANG=en + 영어 강제 + override-defense 키워드 포함.
    last_block = content.rsplit("\n\n", 1)[-1]
    assert last_block.startswith("[LANG=en")
    assert "English" in last_block
    assert "IGNORE" in last_block  # override defense marker
    assert last_block.endswith("]")


@pytest.mark.asyncio
async def test_system_prompt_persona_and_memory_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_SYSTEM_PROMPT` / `_PROACTIVE_DIRECTIVE` 본문은 보존."""
    from app.agents import react_loop as rl

    content = await _run_and_capture(monkeypatch, lang="en")
    # persona system prompt body present, proactive directive present.
    assert rl._SYSTEM_PROMPT in content
    assert rl._PROACTIVE_DIRECTIVE in content
    # directive is appended (last block), not interleaved.
    last_block = content.rsplit("\n\n", 1)[-1]
    assert last_block.startswith("[LANG=en")
    assert last_block.endswith("]")
    # `_PROACTIVE_DIRECTIVE` appears BEFORE the directive.
    assert content.index(rl._PROACTIVE_DIRECTIVE) < content.index(last_block)


@pytest.mark.asyncio
async def test_sticky_lang_across_button_tap_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """1턴 한글 → session.lang=ko. 2턴 button-tap (text 없음) — directive 여전히 KO."""
    from app.agents import react_loop as rl

    # 1턴: 한글 text 로 session.lang=ko 가 미리 세팅된 상황 (lang.remember_lang 가 webhook 노드에서 처리).
    sess = _make_session(lang="ko")
    # 2턴: callback 만 있는 메시지 (text 없음).
    msg = ChannelMessage(
        chat_id=42,
        text=None,
        callback_data="cards:more",
        received_at=datetime.now(UTC),
    )
    state = WorkingState(message=msg, chat_id=42, from_user_id=99)

    fake = _CapturingLLM()
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    await rl.run_react_loop(state, sess)
    content = _system_content(fake.captured_messages or [])
    # KO 분기 — 마지막 블록은 [LANG=ko ...반말... 해요체...] 형태로 끝남.
    last_block = content.rsplit("\n\n", 1)[-1]
    assert last_block.startswith("[LANG=ko")
    assert "반말" in last_block
