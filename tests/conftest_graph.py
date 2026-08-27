"""SPEC-AGENT-001 — shared test doubles for graph node + flow tests.

Lifted from the deleted `tests/test_scenario.py` (REQ-MIGR-001) — same
FakeAdapter / StubPort / FakeCandidate shapes the prior suite used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.channels.recommendation import (
    ChannelRecommendationRequest,
    ChannelRecommendationResult,
)
from app.channels.schemas import ChannelMessage


class FakeAdapter:
    def __init__(self) -> None:
        self.texts: list[tuple[int, str]] = []
        self.cards: list = []
        self.actions: list[tuple[int, str]] = []
        self.buttons: list[tuple[int, str, list]] = []
        self.callback_answers: list[tuple[str, str | None]] = []
        self.bytes_payload: bytes | None = None
        # SPEC-CONVERSATION-LOG-001 / LOG-T17 — send_card now returns
        # ``int | None`` (channel message_id). Default mimics success with
        # an incrementing message_id so tests that previously asserted on
        # truthy returns keep passing.
        self.send_card_returns: int | None = 1001
        self._next_message_id = 1001

    async def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))

    async def send_card(self, chat_id, card):
        self.cards.append((chat_id, card))
        if self.send_card_returns is None or self.send_card_returns is False:
            return None
        # If a fixed int was set, return it once; if True (legacy), produce a
        # fresh per-call int so multi-card tests get distinct message_ids.
        if self.send_card_returns is True:
            mid = self._next_message_id
            self._next_message_id += 1
            return mid
        return self.send_card_returns

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))

    async def send_text_with_buttons(self, chat_id, text, buttons):
        self.buttons.append((chat_id, text, list(buttons)))

    async def answer_callback_query(self, cbq_id, text=None):
        self.callback_answers.append((cbq_id, text))

    async def download_attachment(self, file_id):
        return self.bytes_payload or b"\x00\x01"

    async def parse_inbound(self, payload):  # unused
        raise NotImplementedError


@dataclass
class FakeCandidate:
    id: str = "fake-1"
    image_url: str | None = "https://img.example.com/a.jpg"
    product_url: str | None = "https://shop.example.com/p/1"
    brand: str = "BrandX"
    name: str = "Slim Tee"
    platform: str = "BrandX"
    subcategory: str = "tops"
    price: int | None = 49000


class StubPort:
    def __init__(self, candidates: list[FakeCandidate] | None = None) -> None:
        self.candidates = candidates if candidates is not None else [FakeCandidate()]
        self.calls: list[ChannelRecommendationRequest] = []

    async def recommend(self, req: ChannelRecommendationRequest) -> ChannelRecommendationResult:
        self.calls.append(req)
        return ChannelRecommendationResult(candidates=list(self.candidates))


class StubLLMResult:
    def __init__(self, content: str) -> None:
        self.content = content


class StubLLM:
    """Replacement for ChatOpenAI in node tests. Returns a fixed string."""

    def __init__(self, content: str = "Got it — here are some matches.") -> None:
        self.content = content
        self.calls: list[Any] = []

    async def ainvoke(self, messages, **kw):
        self.calls.append(messages)
        return StubLLMResult(self.content)


def make_msg(
    chat_id: int = 42,
    text: str | None = None,
    photo_file_id: str | None = None,
    urls: list[str] | None = None,
    callback_data: str | None = None,
    from_user_id: int | None = None,
) -> ChannelMessage:
    cbq_id = "cbq-test-1" if callback_data else None
    return ChannelMessage(
        chat_id=chat_id,
        from_user_id=from_user_id,
        text=text,
        photo_file_id=photo_file_id,
        urls=urls or [],
        callback_data=callback_data,
        callback_query_id=cbq_id,
        received_at=datetime.now(tz=UTC),
    )
