from abc import ABC, abstractmethod

from app.channels.schemas import BotCard, ChannelMessage


class MessengerAdapter(ABC):
    """Transport-neutral output interface used by the graph.

    The native app/web path binds ``StreamingAdapter`` (or ``CaptureAdapter``
    for batch calls) per invocation.  The integer recipient argument remains
    named ``chat_id`` in this low-level protocol for graph compatibility; it is
    a session key, not a Telegram/API identifier.
    """

    @abstractmethod
    async def parse_inbound(self, payload: dict) -> ChannelMessage: ...

    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...

    @abstractmethod
    async def send_card(self, chat_id: int, card: BotCard) -> int | None:
        """Send a card.

        Returns the platform message_id on success, ``None`` on failure.
        SPEC-CONVERSATION-LOG-001 / LOG-T17 requires message_id propagation
        for callback thread_id correlation (REQ-LOG-THREAD-CALLBACK-001).
        """
        ...

    # SPEC-AGENT-UX-P0-001 / REQ-UX-003 — optional typing indicator.
    # Default no-op so adapters without a typing surface 자동 skip. 구현체는
    # 성공 시 True, 실패/미구현 시 False 를 반환한다 (fail-open contract).
    async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        return False

    # Non-visible progress heartbeat for long-running steps (vision extract, etc).
    # StreamingAdapter forwards to the SSE queue so the mobile client can reset
    # its stall-timeout. Other adapters no-op.
    async def send_progress(self, chat_id: int, stage: str) -> bool:
        return False

    # @MX:SPEC: SPEC-ONBOARD-CARDS-001 — multi-row inline keyboard for cards
    # with more than 4 buttons (e.g. 8-option mood card in 4 rows of 2 + footer).
    async def send_text_with_keyboard(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[tuple[str, str]]],
    ) -> int | None:
        """Send text with a multi-row inline keyboard.

        `keyboard` is a list of rows, each row a list of (label, callback_data).
        Returns the platform message_id on success (for editMessageReplyMarkup
        re-render on toggle), or None on failure. Default implementation falls
        back to `send_text` so legacy adapters keep working — concrete adapters
        SHOULD override.
        """
        await self.send_text(chat_id, text)
        return None

    async def send_media_group(self, chat_id: int, media: list[dict]) -> bool:
        """Send 2..10 photos as a SINGLE grouped message (one bubble).

        `media` is a list of dicts, each `{"image_url": str, "caption": str|None,
        "parse_mode": str|None}`. Media groups do NOT support per-photo inline
        keyboards — the caller pairs this with a
        follow-up summary message that carries the keyboard.

        The operation is ATOMIC: one bad photo URL fails the whole
        group. Returns True only when the platform reports success; callers
        MUST fall back to per-card `send_card` on a False return so a search
        never yields zero cards.

        Default implementation returns False so backends that have not
        implemented grouped sends transparently degrade to the per-card path.
        """
        return False

    async def download_attachment(self, file_id: str) -> bytes:
        raise NotImplementedError("download_attachment is backend-specific")

    async def get_me(self) -> dict:
        raise NotImplementedError("get_me is backend-specific")

    async def aclose(self) -> None:
        return None
