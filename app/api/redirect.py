"""Outbound redirect proxy for CTR measurement.

`GET /r/{token}` looks up a click token (minted by respond.send_hybrid_batch
when a card is shown), emits a `card_clicked` conversation-log event bound to
the original recommendation trace, and 302-redirects to the real product URL.

Why this exists: Telegram opens external URLs directly from user's device,
bypassing the AI server. Wrapping URLs through this proxy is the only way to
measure click-through rate on cards. Without it, `card_impression.click_status`
relies on expired-attribution heuristics only (`attribute_expired_impressions`).

No auth — tokens are unguessable (96-bit entropy). On miss/expiry the user
sees a friendly 404, never a stack trace. Public endpoint by design (lives
on the same host as Telegram webhook).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, RedirectResponse

from app.infrastructure.cache.click_token import lookup_token
from app.observability.conversation_log import emit

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/r/{token}", include_in_schema=False)
async def outbound_redirect(token: str):
    """Resolve token → log click → 302 to product_url. Never raises."""
    info = await lookup_token(token)
    if not info:
        # Miss / expired / Redis down → friendly text. Avoid 404 because some
        # link previews treat 4xx as broken; 410 Gone communicates the intent.
        logger.info("🐱 [redirect] token miss token=%s…", token[:6])
        return PlainTextResponse(
            "This link has expired. Please open the recommendation again.",
            status_code=410,
        )

    chat_id = int(info.get("chat_id", 0) or 0)
    rec_id = info.get("rec_id") or None
    product_id = str(info.get("product_id") or "")
    product_url = str(info.get("product_url") or "")

    if not product_url:
        logger.warning("[redirect] token resolved without product_url token=%s…", token[:6])
        return PlainTextResponse("This link is no longer available.", status_code=410)

    # Fire-and-forget click event. `rec_id` (= langfuse_trace) is what binds
    # this click back to the recommendation batch in SQL analytics.
    try:
        emit(
            event_type="card_clicked",
            user_key="-",  # PII-free path: no per-user attribution from URL tap
            chat_id=chat_id,
            thread_id=None,
            turn_no=None,
            payload={"product_id": product_id, "position": None, "dwell_ms": None},
            langfuse_trace=rec_id,
        )
    except Exception:  # noqa: BLE001 — observability is best-effort
        logger.debug("[redirect] emit best-effort skip", exc_info=True)

    logger.info("🐱 [redirect] hit token=%s… product=%s", token[:6], product_id[:12])
    return RedirectResponse(url=product_url, status_code=302)
