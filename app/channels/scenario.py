"""Seven-state scenario machine driving the messenger conversation."""

import hashlib
import logging
import uuid
from typing import Any

from pydantic import ValidationError

from app.channels import link_resolver, vision
from app.channels.adapter import MessengerAdapter
from app.channels.schemas import BotCard, ChannelMessage
from app.channels.session import Session, SessionState, get_store
from app.models.request import AnalyzedItem, RecommendRequest
from app.observability.langfuse import observe
from app.pipeline.runner import run_pipeline

logger = logging.getLogger(__name__)

OPENER_TMPL = "Such a cool {item}! What are you looking for — same vibe, something cheaper, or a specific color?"
ZERO_RESULT = "Hmm, I couldn't find a match — try another angle or a different photo."
CLOSER = "Tap any to see more like it ✨"
LINK_FAIL = "Sorry, couldn't load that. Try sharing the photo directly."
TEXT_ONLY = "Send me a photo first 📸"

_MAX_CARDS = 5
_MIN_CARDS = 4


def _hash_chat_id(chat_id: int) -> str:
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def _build_query(intent: str | None, keywords: list[str]) -> str:
    parts = [(intent or "").strip(), " ".join(keywords).strip()]
    q = " ".join(p for p in parts if p).strip().lower()
    return q[:256]


def _build_recommend_request(image_url: str, intent: str | None, vision_data: dict) -> RecommendRequest:
    keywords: list[str] = vision_data.get("keywords") or []
    query = _build_query(intent, keywords)
    item = AnalyzedItem(
        id=f"telegram-{uuid.uuid4().hex[:12]}",
        category=(vision_data.get("item") or "item"),
        subcategory=None,
        name=vision_data.get("item"),
        color_family=(vision_data.get("color") or None),
        search_query=query or (vision_data.get("item") or "fashion item"),
        search_query_ko=None,
    )
    return RecommendRequest(
        item=item,
        image_url=image_url,
        tolerance=0.5,
    )


def _candidate_to_card(c: Any) -> BotCard | None:
    image_url = getattr(c, "image_url", None)
    product_url = getattr(c, "product_url", None)
    brand = getattr(c, "brand", "") or ""
    price = getattr(c, "price", None)
    if not image_url or not product_url:
        return None
    if price is None:
        caption = brand or "Recommended"
    else:
        caption = f"{brand}\n{price}".strip() or "Recommended"
    try:
        return BotCard(
            image_url=image_url,
            caption=caption,
            button_text="View",
            button_url=product_url,
        )
    except ValidationError:
        return None


async def _send_results(adapter: MessengerAdapter, chat_id: int, candidates: list) -> int:
    sent = 0
    for c in candidates[:_MAX_CARDS]:
        card = _candidate_to_card(c)
        if card is None:
            continue
        try:
            await adapter.send_card(chat_id, card)
            sent += 1
        except Exception:
            logger.exception("send_card failed chat=%s", _hash_chat_id(chat_id))
            break
        if sent >= _MAX_CARDS:
            break
    return sent


async def _resolve_image_for_message(message: ChannelMessage, adapter: MessengerAdapter) -> str | bytes | None:
    """Return either an image URL string, raw bytes, or None on failure."""
    if message.photo_file_id:
        try:
            return await adapter.download_attachment(message.photo_file_id)
        except Exception:
            logger.exception("download_attachment failed chat=%s", _hash_chat_id(message.chat_id))
            return None
    if message.urls:
        for u in message.urls:
            images = await link_resolver.resolve(str(u))
            if images:
                return images[0]
        return None
    return None


@observe(name="scenario_handle")
async def handle(adapter: MessengerAdapter, message: ChannelMessage) -> None:
    chat_id = message.chat_id
    chat_hash = _hash_chat_id(chat_id)
    store = get_store()
    lock = store.lock_for(chat_id)
    async with lock:
        session = store.get_or_create(chat_id)
        logger.info("scenario start chat=%s state=%s", chat_hash, session.state.value)

        # AWAITING_INTENT: any user text reply (not photo, not URL-only) -> SEARCHING
        if session.state == SessionState.AWAITING_INTENT and message.text and not message.photo_file_id:
            session.user_intent = message.text.strip()
            session.state = SessionState.SEARCHING
            store.update(session)
            await _run_search(adapter, session, chat_hash)
            return

        # Photo or URL inbound -> reset and start a new image flow
        has_photo = bool(message.photo_file_id)
        has_url = bool(message.urls)

        if has_photo or has_url:
            if has_url and not has_photo:
                session.state = SessionState.LINK_RESOLUTION
            else:
                session.state = SessionState.VISION_PROCESSING
            session.image_url = None
            session.vision_keywords = []
            session.vision_item = None
            session.user_intent = None
            store.update(session)

            try:
                await adapter.send_chat_action(chat_id, "typing")
            except Exception:
                pass

            image = await _resolve_image_for_message(message, adapter)
            if image is None:
                await adapter.send_text(chat_id, LINK_FAIL)
                session.state = SessionState.IDLE
                store.update(session)
                return

            if isinstance(image, str):
                session.image_url = image

            session.state = SessionState.VISION_PROCESSING
            store.update(session)
            try:
                await adapter.send_chat_action(chat_id, "typing")
            except Exception:
                pass

            vision_data = await vision.extract(image)
            session.vision_keywords = vision_data.get("keywords", [])
            session.vision_item = vision_data.get("item") or None

            opener_item = session.vision_item or "piece"
            await adapter.send_text(chat_id, OPENER_TMPL.format(item=opener_item))
            session.state = SessionState.AWAITING_INTENT
            store.update(session)
            return

        # Text-only without URL in IDLE (or anywhere else) -> nudge for photo
        if message.text and session.state in (SessionState.IDLE, SessionState.RESULTS_SENT):
            await adapter.send_text(chat_id, TEXT_ONLY)
            return

        # Fallback: ignore silently
        logger.info("scenario noop chat=%s state=%s", chat_hash, session.state.value)


async def _run_search(adapter: MessengerAdapter, session: Session, chat_hash: str) -> None:
    chat_id = session.chat_id
    store = get_store()

    if not session.image_url:
        # Vision was driven from bytes; we have keywords but no URL — pipeline requires image_url.
        # In that case we cannot run the existing pipeline as-is. Fall back to friendly error.
        logger.warning("search aborted no image_url chat=%s", chat_hash)
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return

    vision_data = {
        "item": session.vision_item or "item",
        "color": "",
        "keywords": session.vision_keywords,
    }
    try:
        req = _build_recommend_request(session.image_url, session.user_intent, vision_data)
    except ValidationError:
        logger.exception("recommend request build failed chat=%s", chat_hash)
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return

    try:
        resp = await run_pipeline(req)
    except Exception:
        logger.exception("pipeline failed chat=%s", chat_hash)
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return

    candidates = list(resp.results) if resp and resp.results else []
    if not candidates:
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return

    sent = await _send_results(adapter, chat_id, candidates)
    if sent == 0:
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return

    if sent < _MIN_CARDS:
        logger.info("scenario sent below minimum chat=%s sent=%d", chat_hash, sent)

    await adapter.send_text(chat_id, CLOSER)
    session.state = SessionState.RESULTS_SENT
    store.update(session)
