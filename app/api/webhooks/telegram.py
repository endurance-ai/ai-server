"""Telegram webhook endpoint — verifies secret token, normalizes payload, kicks off scenario."""

import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi.responses import ORJSONResponse

from app.channels import scenario
from app.channels.factory import get_adapter
from app.channels.schemas import ChannelParseError
from app.channels.telegram.webhook import verify_secret_token
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/telegram", tags=["webhooks"])


@router.post("")
@router.post("/")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> ORJSONResponse:
    expected = settings.TELEGRAM_WEBHOOK_SECRET
    if not verify_secret_token(x_telegram_bot_api_secret_token, expected):
        client_host = request.client.host if request.client else "unknown"
        logger.warning("telegram webhook rejected: bad secret token from %s", client_host)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    try:
        payload = await request.json()
    except Exception:
        logger.warning("telegram webhook: invalid JSON")
        return ORJSONResponse({"ok": True})

    adapter = get_adapter()
    try:
        message = await adapter.parse_inbound(payload)
    except ChannelParseError as e:
        logger.error("telegram parse_inbound error: %s | payload=%s", e, payload)
        return ORJSONResponse({"ok": True})
    except Exception:
        logger.exception("telegram parse_inbound unexpected error | payload=%s", payload)
        return ORJSONResponse({"ok": True})

    logger.info(
        "inbound parsed text=%r photo_file_id=%s urls=%s",
        (message.text or "")[:80],
        message.photo_file_id,
        [str(u) for u in message.urls],
    )

    background_tasks.add_task(_run_scenario_safe, adapter, message)
    return ORJSONResponse({"ok": True})


async def _run_scenario_safe(adapter, message) -> None:
    try:
        await scenario.handle(adapter, message)
    except Exception:
        logger.exception("scenario.handle background task failed")
