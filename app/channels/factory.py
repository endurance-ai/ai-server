"""MessengerAdapter factory — env-driven selection."""

import logging

from app.channels.adapter import MessengerAdapter
from app.channels.telegram.adapter import TelegramAdapter
from app.core.config import settings

logger = logging.getLogger(__name__)

_ACCEPTED = {"telegram", "bluebubbles", "sendblue"}

_instance: MessengerAdapter | None = None


class _StubAdapter(MessengerAdapter):
    def __init__(self, name: str) -> None:
        self._name = name

    def _raise(self) -> None:
        raise NotImplementedError(f"{self._name} adapter is a P3 stub")

    async def parse_inbound(self, payload: dict):
        self._raise()

    async def send_text(self, chat_id: int, text: str) -> None:
        self._raise()

    async def send_card(self, chat_id, card) -> bool:
        self._raise()
        return False

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self._raise()

    async def get_me(self) -> dict:
        return {}


def build_adapter() -> MessengerAdapter:
    backend = settings.MESSENGER_BACKEND.lower().strip()
    if backend not in _ACCEPTED:
        raise ValueError(f"MESSENGER_BACKEND must be one of {sorted(_ACCEPTED)}; got {backend!r}")
    if backend == "telegram":
        token = settings.TELEGRAM_BOT_TOKEN
        api_base = settings.TELEGRAM_API_BASE
        if not token:
            logger.warning(
                "MESSENGER_BACKEND=telegram but TELEGRAM_BOT_TOKEN is not set; adapter will fail on first call"
            )
            return _StubAdapter("telegram-uninitialized")
        return TelegramAdapter(bot_token=token, api_base=api_base)
    return _StubAdapter(backend)


def get_adapter() -> MessengerAdapter:
    global _instance
    if _instance is None:
        _instance = build_adapter()
    return _instance


def set_adapter(adapter: MessengerAdapter) -> None:
    global _instance
    _instance = adapter


async def reset_adapter() -> None:
    global _instance
    if _instance is not None:
        try:
            await _instance.aclose()
        except Exception:
            pass
        _instance = None


def backend_name() -> str:
    return settings.MESSENGER_BACKEND.lower().strip()
