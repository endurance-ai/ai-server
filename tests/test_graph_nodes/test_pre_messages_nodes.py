"""SPEC-AGENT-UX-P0-001 v0.2.0 / REQ-UX-004 — graph 노드 pre-message firing.

vision_node 진입 시 PRE_MESSAGES["vision"][lang] 멘트가 Vision LLM 호출
직전 1회 발사되는지 검증.

pinterest_ingest 노드는 현재 부재 (SPEC-ONBOARD-LITE-001 §4 에서 제거).
재도입 시 동일 패턴으로 노드 entry 직후 fire_pre_message 호출 + 본 테스트
파일에 vision 케이스와 동일한 형태로 케이스 추가.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
        raise RuntimeError("simulated telegram failure")

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:  # pragma: no cover
        return None


@pytest.fixture(autouse=True)
def _reset_markers() -> None:
    _reset_node_markers_for_tests()


def _make_state(chat_id: int = 42, image_url: str = "https://cdn.example.com/x.jpg"):
    from app.graphs.state import WorkingState

    msg = ChannelMessage(chat_id=chat_id, text="hi", received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=chat_id, from_user_id=99, image_url=image_url)


@pytest.fixture
def _stub_vision(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """vision_module.extract 를 stub. 호출 순서 추적용 sentinel list 반환."""
    call_log: list[str] = []

    async def _fake_extract(url: str) -> Any:
        call_log.append("vision_extract")
        # legacy dict — vision_node 의 isinstance(result, dict) 분기로 자동 coerce.
        return {
            "isApparel": True,
            "items": [],
            "mood": {"tags": []},
            "palette": [],
            "style": {"aesthetic": None, "detectedGender": None},
            "styleNode": {"primary": None, "secondary": None},
            "sensitivityTags": [],
        }

    from app.channels import vision as vision_module

    monkeypatch.setattr(vision_module, "extract", _fake_extract)
    return call_log


def _bind_adapter(monkeypatch: pytest.MonkeyPatch, adapter: MessengerAdapter) -> None:
    # vision_node 가 직접 _adapter_var.get() 을 호출하므로 ContextVar 에 set.
    from app.graphs.nodes import _adapter_ctx

    token = _adapter_ctx.set_adapter(adapter)
    # auto-reset at end of test via finalizer.
    monkeypatch.setattr(_adapter_ctx, "_TEST_TOKEN", token, raising=False)


# ── vision_node fires pre-message ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_vision_node_fires_pre_message_ko(monkeypatch: pytest.MonkeyPatch, _stub_vision: list[str]) -> None:
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)

    # session.lang 을 KO 로.
    from app.infrastructure.memory.session import get_store

    sess = get_store().get_or_create(42)
    sess.lang = "ko"
    get_store().update(sess)

    from app.graphs.nodes.vision import vision_node

    await vision_node(_make_state())
    # 첫 send_text 가 PRE_MESSAGES["vision"]["ko"], 그 다음에 vision_extract 호출.
    assert spy.texts == [(42, PRE_MESSAGES["vision"]["ko"])]
    assert _stub_vision == ["vision_extract"]


@pytest.mark.asyncio
async def test_vision_node_fires_pre_message_en(monkeypatch: pytest.MonkeyPatch, _stub_vision: list[str]) -> None:
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)

    from app.infrastructure.memory.session import get_store

    sess = get_store().get_or_create(42)
    sess.lang = "en"
    get_store().update(sess)

    from app.graphs.nodes.vision import vision_node

    await vision_node(_make_state())
    assert spy.texts == [(42, PRE_MESSAGES["vision"]["en"])]


@pytest.mark.asyncio
async def test_vision_node_skips_pre_message_when_no_image(
    monkeypatch: pytest.MonkeyPatch, _stub_vision: list[str]
) -> None:
    """no image_url → early-return 으로 pre-message 도 건너뛴다 (의도된 동작)."""
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)

    from app.graphs.nodes.vision import vision_node

    state = _make_state()
    state.image_url = None
    await vision_node(state)
    assert spy.texts == []  # early return 이전에는 fire 하지 않음
    assert _stub_vision == []


@pytest.mark.asyncio
async def test_vision_node_idempotent_within_same_thread(
    monkeypatch: pytest.MonkeyPatch, _stub_vision: list[str]
) -> None:
    """같은 thread_id 로 두 번 호출 → pre-message 1회만."""
    spy = _SpyAdapter()
    _bind_adapter(monkeypatch, spy)

    from app.graphs.nodes.vision import vision_node

    state = _make_state()
    await vision_node(state)
    await vision_node(state)  # 같은 thread_id 재진입
    assert len(spy.texts) == 1


@pytest.mark.asyncio
async def test_vision_node_send_text_failure_does_not_block_extract(
    monkeypatch: pytest.MonkeyPatch, _stub_vision: list[str]
) -> None:
    """send_text 가 raise → Vision extract 는 그대로 실행."""
    raising = _RaisingTextAdapter()
    _bind_adapter(monkeypatch, raising)

    from app.graphs.nodes.vision import vision_node

    await vision_node(_make_state())
    assert raising.send_text_called == 1
    assert _stub_vision == ["vision_extract"]
