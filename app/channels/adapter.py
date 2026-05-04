from abc import ABC, abstractmethod

from app.channels.schemas import BotCard, ChannelMessage


class MessengerAdapter(ABC):
    """Backend-neutral messenger interface. Concrete implementations live under
    `app/channels/<backend>/adapter.py`. Selected at startup via MESSENGER_BACKEND env.
    """

    @abstractmethod
    async def parse_inbound(self, payload: dict) -> ChannelMessage: ...

    @abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None: ...

    @abstractmethod
    async def send_card(self, chat_id: int, card: BotCard) -> bool: ...

    @abstractmethod
    async def send_chat_action(self, chat_id: int, action: str) -> None: ...

    async def download_attachment(self, file_id: str) -> bytes:
        raise NotImplementedError("download_attachment is backend-specific")

    async def get_me(self) -> dict:
        raise NotImplementedError("get_me is backend-specific")

    async def aclose(self) -> None:
        return None
