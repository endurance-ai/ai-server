"""Telegram Bot API adapter — async httpx, no telegram SDK."""

import asyncio
import hashlib
import logging
import time
from datetime import UTC, datetime

import httpx
from pydantic import ValidationError

from app.channels.adapter import MessengerAdapter
from app.channels.schemas import BotCard, ChannelMessage, ChannelParseError
from app.core.config import settings as _settings  # noqa: F401  (env-driven, kept for parity)

logger = logging.getLogger(__name__)


def _hash_chat_id(chat_id: int) -> str:
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


class TelegramAdapter(MessengerAdapter):
    def __init__(self, bot_token: str, api_base: str = "https://api.telegram.org") -> None:
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required for TelegramAdapter")
        self._token = bot_token
        self._api_base = api_base.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def api_base(self) -> str:
        return self._api_base

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    def _bot_url(self, method: str) -> str:
        return f"{self._api_base}/bot{self._token}/{method}"

    def file_url(self, file_path: str) -> str:
        return f"{self._api_base}/file/bot{self._token}/{file_path}"

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, method: str, payload: dict, retry_429: bool = True) -> dict | None:
        client = self._get_client()
        url = self._bot_url(method)
        try:
            resp = await client.post(url, json=payload)
        except httpx.HTTPError as e:
            logger.error("telegram %s http error: %s", method, e)
            return None
        if resp.status_code == 429 and retry_429:
            try:
                body = resp.json()
                retry_after = float(body.get("parameters", {}).get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            logger.warning("telegram %s 429, retry_after=%.1fs", method, retry_after)
            await asyncio.sleep(retry_after)
            return await self._post(method, payload, retry_429=False)
        if resp.status_code >= 400:
            logger.error("telegram %s status=%d body=%s", method, resp.status_code, resp.text[:300])
            return None
        try:
            return resp.json()
        except Exception:
            return None

    async def send_text(self, chat_id: int, text: str) -> None:
        t0 = time.perf_counter()
        await self._post("sendMessage", {"chat_id": chat_id, "text": text})
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.info("telegram send_text chat=%s elapsed_ms=%d", _hash_chat_id(chat_id), elapsed)

    async def send_card(self, chat_id: int, card: BotCard) -> None:
        t0 = time.perf_counter()
        payload = {
            "chat_id": chat_id,
            "photo": str(card.image_url),
            "caption": card.caption,
            "reply_markup": {
                "inline_keyboard": [[{"text": card.button_text, "url": str(card.button_url)}]],
            },
        }
        await self._post("sendPhoto", payload)
        elapsed = int((time.perf_counter() - t0) * 1000)
        logger.info("telegram send_card chat=%s elapsed_ms=%d", _hash_chat_id(chat_id), elapsed)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        await self._post("sendChatAction", {"chat_id": chat_id, "action": action})

    async def get_me(self) -> dict:
        client = self._get_client()
        try:
            resp = await client.post(self._bot_url("getMe"), timeout=3.0)
            if resp.status_code == 200:
                body = resp.json()
                if body.get("ok"):
                    return body.get("result", {})
        except Exception as e:
            logger.warning("telegram getMe failed: %s", e)
        return {}

    async def download_attachment(self, file_id: str) -> bytes:
        client = self._get_client()
        info = await self._post("getFile", {"file_id": file_id})
        if not info or not info.get("ok"):
            raise RuntimeError("getFile failed")
        file_path = info["result"]["file_path"]
        url = self.file_url(file_path)
        resp = await client.get(url, timeout=15.0)
        resp.raise_for_status()
        return resp.content

    async def parse_inbound(self, payload: dict) -> ChannelMessage:
        message = payload.get("message") or payload.get("edited_message") or payload.get("channel_post")
        if not message or not isinstance(message, dict):
            raise ChannelParseError("payload has no message")

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            raise ChannelParseError("missing chat.id")

        from_user = message.get("from") or {}
        from_user_id = from_user.get("id") if isinstance(from_user.get("id"), int) else None
        from_username = from_user.get("username") if isinstance(from_user.get("username"), str) else None

        text = message.get("text") or message.get("caption")

        photo_file_id: str | None = None
        photos = message.get("photo") or []
        if isinstance(photos, list) and photos:
            largest = max(photos, key=lambda p: int(p.get("file_size") or 0))
            photo_file_id = largest.get("file_id")

        urls: list[str] = []
        entity_sources = (
            ("entities", message.get("text") or ""),
            ("caption_entities", message.get("caption") or ""),
        )
        for entities_key, base_text in entity_sources:
            ents = message.get(entities_key) or []
            if not isinstance(ents, list):
                continue
            for ent in ents:
                if not isinstance(ent, dict):
                    continue
                etype = ent.get("type")
                if etype == "url":
                    offset = int(ent.get("offset") or 0)
                    length = int(ent.get("length") or 0)
                    if base_text and 0 <= offset < len(base_text) and length > 0:
                        urls.append(base_text[offset : offset + length])
                elif etype == "text_link":
                    href = ent.get("url")
                    if isinstance(href, str):
                        urls.append(href)

        ts = message.get("date")
        try:
            received_at = datetime.fromtimestamp(int(ts), tz=UTC) if ts else datetime.now(tz=UTC)
        except (TypeError, ValueError):
            received_at = datetime.now(tz=UTC)

        try:
            return ChannelMessage(
                chat_id=chat_id,
                from_user_id=from_user_id,
                from_username=from_username,
                text=text,
                photo_file_id=photo_file_id,
                urls=urls,
                received_at=received_at,
            )
        except ValidationError as e:
            raise ChannelParseError(str(e)) from e
