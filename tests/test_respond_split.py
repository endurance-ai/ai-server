"""noscroll 벤치마크 — respond 노드 문장 분할 발화 테스트.

대화감을 위해 LLM 출력/fallback 텍스트를 문장 단위로 잘라 순차 전송한다.
각 청크 직전에 typing action 을 한 번씩 보낸다.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.graphs.nodes.respond import _split_into_chunks


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 단문 — 그대로 1개.
        ("hello", ["hello"]),
        # 영어 2문장.
        (
            "Got it. Looking now.",
            ["Got it. Looking now."],  # 첫 청크는 너무 짧아서 다음과 병합됨.
        ),
        # 영어 3문장 — 모두 길이 충분 → 3개.
        (
            "I see the vibe. Searching matching pieces. One sec.",
            ["I see the vibe.", "Searching matching pieces.", "One sec."],
        ),
        # 한국어 2문장.
        (
            "찾아볼게요. 잠시만요.",
            ["찾아볼게요. 잠시만요."],  # "잠시만요." 너무 짧아 병합.
        ),
        # 한국어 3문장 + 영어 mix.
        (
            "느낌 좋네요. 비슷한 거 찾아볼게요. 잠시만 기다려주세요.",
            ["느낌 좋네요. 비슷한 거 찾아볼게요.", "잠시만 기다려주세요."],
        ),
        # 줄바꿈 2개 = 강제 분할.
        ("first chunk here\n\nsecond chunk here", ["first chunk here", "second chunk here"]),
        # 공백/None.
        ("", []),
        ("   ", []),
    ],
)
def test_split_into_chunks(text: str, expected: list[str]) -> None:
    assert _split_into_chunks(text, min_chars=settings.RESPONSE_SPLIT_MIN_CHARS) == expected


def test_split_never_returns_empty_for_nonempty_input() -> None:
    """min_chars 가 매우 커도 입력이 있으면 최소 1청크는 보장."""
    result = _split_into_chunks("a. b. c.", min_chars=999)
    assert len(result) == 1
    assert result[0]  # non-empty


@pytest.mark.asyncio
async def test_respond_emits_typing_then_text_per_chunk(monkeypatch) -> None:
    """3문장 응답이 들어오면 청크가 여러 개로 분리되고 매 청크마다 typing 이 1회씩 선행된다."""
    from app.graphs.nodes import _adapter_ctx
    from app.graphs.nodes import respond as respond_module
    from app.graphs.state import WorkingState
    from app.infrastructure.memory.session import InMemorySessionStore, set_store
    from tests.conftest_graph import FakeAdapter, make_msg

    # 어댑터 / 스토어 주입.
    adapter = FakeAdapter()
    _adapter_ctx.set_adapter(adapter)
    store = InMemorySessionStore()
    set_store(store)

    # LLM 을 길게 답하는 stub 으로 교체 → 3 문장.
    class StubLLM:
        async def ainvoke(self, _messages):
            class _R:
                content = "느낌 좋네요. 비슷한 거 찾아볼게요. 잠시만 기다려주세요."

            return _R()

    monkeypatch.setattr(respond_module, "_get_llm", lambda: StubLLM())
    monkeypatch.setattr(settings, "RESPONSE_SPLIT_DELAY_MS", 0, raising=False)

    state = WorkingState(
        chat_id=42,
        message=make_msg(chat_id=42, text="아무거나 추천"),
    )
    await respond_module.respond(state)

    assert len(adapter.texts) >= 2, f"expected multi-chunk send, got {adapter.texts}"
    typing_count = sum(1 for _, action in adapter.actions if action == "typing")
    assert typing_count == len(adapter.texts), (
        f"typing count ({typing_count}) must match chunk count ({len(adapter.texts)})"
    )
