"""Telegram webhook endpoint — verifies secret token, normalizes payload,
invokes the LangGraph fashion bot.

SPEC-AGENT-001 (REQ-MIGR-004): replaces the prior call to
`app.channels.scenario.handle(...)` with `await GRAPH.ainvoke(...)`. Channel
adapter, secret-token verification, HTTP 200/401 contract, and parse error
handling are all preserved (REQ-COMPAT-009 / SPEC-MSG-001 REQ-MSG-001/002).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi.responses import ORJSONResponse

from app.channels.adapter import MessengerAdapter
from app.channels.factory import get_adapter
from app.channels.schemas import ChannelMessage, ChannelParseError
from app.channels.telegram.webhook import verify_secret_token
from app.core.config import settings
from app.graphs.fashion_bot import GRAPH
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import InputState
from app.observability.langfuse import build_callback_handler, observe, update_current_trace
from app.observability.pii import hash_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/telegram", tags=["webhooks"])


def _classify_flow(message: ChannelMessage) -> str:
    """REQ-OBS-METADATA-001 — `flow` classifier.

    Returns one of `"image"`, `"callback"`, `"text"`. Image trumps callback
    when both happen to be present; the bot's actual handling is image-first.
    """
    if message.photo_file_id or message.urls:
        return "image"
    if message.callback_data:
        return "callback"
    return "text"


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
        logger.warning("📥 [webhook] 🚫 rejected: bad secret token from %s", client_host)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    try:
        payload = await request.json()
    except Exception:
        logger.warning("📥 [webhook] ⚠️  invalid JSON")
        return ORJSONResponse({"ok": True})

    adapter = get_adapter()
    update_id = payload.get("update_id") if isinstance(payload, dict) else None
    try:
        message = await adapter.parse_inbound(payload)
    except ChannelParseError as e:
        logger.error("📥 [webhook] ❌ parse_inbound error update_id=%s err=%s", update_id, e)
        return ORJSONResponse({"ok": True})
    except Exception:
        logger.exception("📥 [webhook] ❌ parse_inbound unexpected error update_id=%s", update_id)
        return ORJSONResponse({"ok": True})

    # Privacy: hash user identity (review P1) and cap text at 80 chars to
    # match the codebase's existing privacy posture (see ingest.py — "Avoid
    # logging raw user text"). from_username is intentionally NOT logged.
    logger.info(
        "📥 [webhook] inbound update_id=%s user=%s text=%r photo=%s urls=%s",
        update_id,
        hash_id(message.from_user_id),
        (message.text or "")[:80],
        bool(message.photo_file_id),
        [str(u) for u in message.urls],
    )

    background_tasks.add_task(_run_graph_safe, adapter, message)
    return ORJSONResponse({"ok": True})


@observe(name="webhook.telegram", as_type="span")
async def _invoke_graph(adapter: MessengerAdapter, message: ChannelMessage) -> None:
    """Single graph invocation — wraps a root Langfuse trace per webhook.

    REQ-OBS-METADATA-001 — every webhook root trace carries `lang`, `flow`,
    `chat_id_hash`, and (on completion) `critique_retry_count`.
    """
    token = set_adapter(adapter)
    try:
        input_state = InputState(
            message=message,
            chat_id=message.chat_id,
            from_user_id=message.from_user_id,
        )
        session_id = hash_id(message.chat_id)
        user_id = hash_id(message.from_user_id)
        flow = _classify_flow(message)
        # Attach root-trace metadata via v3 client API. `lang` reflects what we
        # will reply with; it's set by `ingest` and overwritten by `respond`
        # before completion — at this entry we attach `flow` + `chat_id_hash`
        # which are knowable from the inbound message alone.
        update_current_trace(
            metadata={
                "flow": flow,
                "chat_id_hash": session_id,
                "channel": "telegram",
                "graph": "fashion_bot",
            }
        )
        handler = build_callback_handler(
            session_id=session_id,
            user_id=user_id,
            metadata={
                "channel": "telegram",
                "graph": "fashion_bot",
                "flow": flow,
            },
        )
        callbacks = [handler] if handler is not None else []
        await GRAPH.ainvoke(input_state, config={"callbacks": callbacks})
    finally:
        reset_adapter(token)


async def _run_graph_safe(adapter: MessengerAdapter, message: ChannelMessage) -> None:
    try:
        await _invoke_graph(adapter, message)
    except Exception:
        logger.exception("📥 [webhook] ❌ fashion_bot graph background task failed")
