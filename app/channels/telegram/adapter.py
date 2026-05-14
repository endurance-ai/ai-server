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
            # 일반 호출 5s. sendPhoto는 별도 timeout으로 짧게 잡음 (per-request override)
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0))
        return self._client

    def _bot_url(self, method: str) -> str:
        return f"{self._api_base}/bot{self._token}/{method}"

    def file_url(self, file_path: str) -> str:
        return f"{self._api_base}/file/bot{self._token}/{file_path}"

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(
        self,
        method: str,
        payload: dict,
        retry_429: bool = True,
        timeout: float | None = None,
    ) -> dict | None:
        client = self._get_client()
        url = self._bot_url(method)
        try:
            if timeout is not None:
                resp = await client.post(url, json=payload, timeout=timeout)
            else:
                resp = await client.post(url, json=payload)
        except httpx.TimeoutException:
            logger.warning("telegram %s ⏱️  타임아웃 (Telegram이 이미지를 가져오지 못함)", method)
            return None
        except httpx.HTTPError as e:
            logger.error("telegram %s ❌ http error: %r", method, e)
            return None
        if resp.status_code == 429 and retry_429:
            try:
                body = resp.json()
                retry_after = float(body.get("parameters", {}).get("retry_after", 1.0))
            except Exception:
                retry_after = 1.0
            logger.warning("telegram %s ⚠️  429 rate-limited retry_after=%.1fs", method, retry_after)
            await asyncio.sleep(retry_after)
            return await self._post(method, payload, retry_429=False, timeout=timeout)
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
                desc = err_body.get("description", "")
            except Exception:
                desc = resp.text[:200]
            logger.warning("telegram %s ❌ status=%d desc=%s", method, resp.status_code, desc)
            return None
        try:
            return resp.json()
        except Exception:
            return None

    async def send_text(self, chat_id: int, text: str) -> None:
        t0 = time.perf_counter()
        await self._post("sendMessage", {"chat_id": chat_id, "text": text})
        elapsed = int((time.perf_counter() - t0) * 1000)
        # 🐱 = kiko가 사용자에게 실제로 내보낸 텍스트
        preview = text.replace("\n", " ⏎ ")
        if len(preview) > 200:
            preview = preview[:200] + "…"
        logger.info(
            "🐱 [telegram] 📤 send_text chat=%s elapsed_ms=%d len=%d msg=%r",
            _hash_chat_id(chat_id),
            elapsed,
            len(text),
            preview,
        )

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:
        """카드 전송. 성공 시 Telegram message_id, 실패 시 None.

        SPEC-CONVERSATION-LOG-001 / LOG-T17 — message_id 를 반환해서 send_results
        가 `card_sent.payload.source_message_id` 로 캡처할 수 있게 한다. 이는
        다음 callback Update 의 thread_id resolution 입력이다 (LOG-T09 lookup).

        sendPhoto timeout 4s — Telegram이 이미지 URL을 가져오는 데 실패하면 빠르게 다음 후보로.
        critique_buttons 가 있으면 Shop 버튼 아래에 한 줄로 추가 송출 (callback_data 기반).
        """
        t0 = time.perf_counter()
        rows: list[list[dict]] = [[{"text": card.button_text, "url": str(card.button_url)}]]
        if card.critique_buttons:
            rows.append(
                [{"text": label, "callback_data": data} for label, data in card.critique_buttons[:4]],
            )
        payload = {
            "chat_id": chat_id,
            "photo": str(card.image_url),
            "caption": card.caption,
            "reply_markup": {"inline_keyboard": rows},
        }
        if card.parse_mode:
            payload["parse_mode"] = card.parse_mode
        result = await self._post("sendPhoto", payload, timeout=4.0)
        elapsed = int((time.perf_counter() - t0) * 1000)
        ok = bool(result and result.get("ok"))
        # LOG-T17 — extract Telegram message_id for callback correlation.
        message_id: int | None = None
        if ok:
            try:
                mid = (result or {}).get("result", {}).get("message_id")
                if isinstance(mid, int):
                    message_id = mid
            except Exception:  # noqa: BLE001
                message_id = None
        cap_preview = (card.caption or "").replace("\n", " ⏎ ")
        if len(cap_preview) > 120:
            cap_preview = cap_preview[:120] + "…"
        logger.info(
            "🐱 [telegram] 🖼️  send_card chat=%s elapsed_ms=%d ok=%s msg_id=%s caption=%r url=%s",
            _hash_chat_id(chat_id),
            elapsed,
            ok,
            message_id,
            cap_preview,
            str(card.button_url),
        )
        return message_id

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        await self._post("sendChatAction", {"chat_id": chat_id, "action": action})

    async def send_text_with_buttons(
        self,
        chat_id: int,
        text: str,
        buttons: list[tuple[str, str]],
    ) -> None:
        """Send a text message with a row of inline callback buttons.
        `buttons` = list of (label, callback_data). Single row, max 4 buttons.
        """
        t0 = time.perf_counter()
        row = [{"text": label, "callback_data": data} for label, data in buttons[:4]]
        payload = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": {"inline_keyboard": [row]},
        }
        await self._post("sendMessage", payload)
        elapsed = int((time.perf_counter() - t0) * 1000)
        btn_labels = [b[0] for b in buttons[:4]]
        text_preview = text.replace("\n", " ⏎ ")
        if len(text_preview) > 120:
            text_preview = text_preview[:120] + "…"
        logger.info(
            "🐱 [telegram] 🔘 send_buttons chat=%s elapsed_ms=%d msg=%r buttons=%s",
            _hash_chat_id(chat_id),
            elapsed,
            text_preview,
            btn_labels,
        )

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        body: dict = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text
        await self._post("answerCallbackQuery", body)

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
        cbq = payload.get("callback_query")
        if isinstance(cbq, dict):
            cbq_message = cbq.get("message") or {}
            chat = cbq_message.get("chat") or {}
            chat_id = chat.get("id")
            if not isinstance(chat_id, int):
                raise ChannelParseError("callback_query missing chat.id")
            from_user = cbq.get("from") or {}
            return ChannelMessage(
                chat_id=chat_id,
                from_user_id=from_user.get("id") if isinstance(from_user.get("id"), int) else None,
                from_username=from_user.get("username") if isinstance(from_user.get("username"), str) else None,
                text=None,
                photo_file_id=None,
                urls=[],
                callback_data=str(cbq.get("data") or ""),
                callback_query_id=str(cbq.get("id") or ""),
                received_at=datetime.now(tz=UTC),
            )

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
            base_utf16 = base_text.encode("utf-16-le") if base_text else b""
            base_utf16_units = len(base_utf16) // 2
            for ent in ents:
                if not isinstance(ent, dict):
                    continue
                etype = ent.get("type")
                if etype == "url":
                    offset = int(ent.get("offset") or 0)
                    length = int(ent.get("length") or 0)
                    if base_utf16 and 0 <= offset < base_utf16_units and length > 0:
                        chunk = base_utf16[offset * 2 : (offset + length) * 2]
                        try:
                            urls.append(chunk.decode("utf-16-le"))
                        except UnicodeDecodeError:
                            continue
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
