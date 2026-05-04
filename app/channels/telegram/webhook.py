"""Telegram webhook helpers — secret-token verify + setWebhook bootstrap."""

import hmac
import logging

from app.channels.telegram.adapter import TelegramAdapter

logger = logging.getLogger(__name__)


def verify_secret_token(header_value: str | None, expected: str) -> bool:
    if not expected or not header_value:
        return False
    return hmac.compare_digest(header_value, expected)


async def setup_webhook(adapter: TelegramAdapter, public_url: str, secret: str) -> None:
    """Register the webhook URL with Telegram. Logs status; does not raise."""
    if not public_url or not secret:
        logger.info("setup_webhook skipped: missing public_url or secret")
        return

    payload = {
        "url": public_url,
        "secret_token": secret,
        "allowed_updates": ["message", "callback_query"],
    }
    resp = await adapter._post("setWebhook", payload)  # noqa: SLF001
    if resp and resp.get("ok"):
        logger.info("telegram setWebhook ok url=%s", public_url)
    else:
        logger.warning("telegram setWebhook failed resp=%s", resp)

    info = await adapter._post("getWebhookInfo", {})  # noqa: SLF001
    if info and info.get("ok"):
        result = info.get("result", {})
        pending = result.get("pending_update_count", 0)
        current_url = result.get("url", "")
        if pending > 0:
            logger.warning("telegram getWebhookInfo pending_update_count=%d", pending)
        if current_url and current_url != public_url:
            logger.warning("telegram webhook URL mismatch current=%s expected=%s", current_url, public_url)
