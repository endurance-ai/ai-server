"""Seven-state scenario machine driving the messenger conversation.

Structure:
    1. Trigger enum + classify_input  — input classification (state-aware)
    2. HandlerContext                 — bundle passed to every handler
    3. Handler functions              — one per (state, trigger) cell
    4. TRANSITIONS table              — (state, trigger) -> handler dict.
                                        state=None means "any state" (wildcard fallback)
    5. handle()                       — dispatcher entry point

Side effects (adapter calls, state writes) live inside handlers — this is
intentional given current side-effect cardinality (send_text / send_card /
send_chat_action / run_search). When/if it grows, lift to a TransitionResult
+ executor pattern.
"""

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from app.channels import link_resolver, vision
from app.channels.adapter import MessengerAdapter
from app.channels.recommendation import (
    ChannelRecommendationRequest,
    RecommendationPort,
    get_port,
)
from app.channels.schemas import BotCard, ChannelMessage
from app.channels.session import Session, SessionState, SessionStore, get_store
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

PICKER_HEADER = "I see {n} item{s} in this photo 👀\n\n{lines}\n\nWhich one are you after? Tap below 👇"
PICKER_LINE = "{num}  {label} — {desc}"
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
OPENER_TMPL = "Got it — {item} 👌\nSame vibe, something cheaper, or a specific color?"
ZERO_RESULT = "Hmm, I couldn't find a match — try another angle or a different photo."
CLOSER = "Tap any to see more like it ✨"
LINK_FAIL = "Sorry, couldn't load that. Try sharing the photo directly."
TEXT_ONLY = "Send me a photo first 📸"
PICK_INVALID = "Tap one of the buttons above to choose an item 👆"

_MAX_CARDS = 5
_MIN_CARDS = 4
_MAX_INTENT_LEN = 512  # session storage cap; downstream query is further trimmed to 256 in PipelineRecommendationPort


def _hash_chat_id(chat_id: int) -> str:
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def _build_channel_request(image_url: str, intent: str | None, vision_data: dict) -> ChannelRecommendationRequest:
    return ChannelRecommendationRequest(
        image_url=image_url,
        item_label=vision_data.get("item"),
        intent=intent,
        keywords=list(vision_data.get("keywords") or []),
        tolerance=0.5,
        color=(vision_data.get("color") or None),
    )


def _format_price(price: Any) -> str | None:
    """89000 → '₩89,000' / None → None."""
    if price is None:
        return None
    try:
        n = int(price)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"₩{n:,}"


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _candidate_to_card(c: Any) -> BotCard | None:
    image_url = getattr(c, "image_url", None)
    product_url = getattr(c, "product_url", None)
    brand = (getattr(c, "brand", "") or "").strip()
    name = (getattr(c, "name", "") or "").strip()
    platform = (getattr(c, "platform", "") or "").strip()
    subcategory = (getattr(c, "subcategory", "") or "").strip()
    price_str = _format_price(getattr(c, "price", None))

    if not image_url or not product_url:
        return None

    # HTML 캡션 구성 (Telegram parse_mode=HTML)
    # 1줄: <b>제품명</b>
    # 2줄: 브랜드 · 카테고리
    # 3줄: 💰 가격  (있으면)
    # 4줄: 🏬 플랫폼  (있으면, 플랫폼이 브랜드와 다를 때만)
    lines: list[str] = []
    if name:
        lines.append(f"<b>{_html_escape(name)}</b>")
    meta_bits: list[str] = []
    if brand:
        meta_bits.append(_html_escape(brand))
    if subcategory:
        meta_bits.append(_html_escape(subcategory))
    if meta_bits:
        lines.append(" · ".join(meta_bits))
    if price_str:
        lines.append(f"💰 <b>{_html_escape(price_str)}</b>")
    if platform and platform.lower() != brand.lower():
        lines.append(f"🏬 {_html_escape(platform)}")

    caption = "\n".join(lines) if lines else "Recommended"
    if len(caption) > 1024:  # Telegram caption 제한
        caption = caption[:1020] + "…"

    button_label = f"🛒  Shop on {brand}" if brand else "🛒  Shop now  →"
    if len(button_label) > 64:  # Telegram 버튼 텍스트 제한
        button_label = "🛒  Shop now  →"

    try:
        return BotCard(
            image_url=image_url,
            caption=caption,
            button_text=button_label,
            button_url=product_url,
            parse_mode="HTML",
        )
    except ValidationError:
        return None


async def _send_results(adapter: MessengerAdapter, chat_id: int, candidates: list) -> int:
    sent = 0
    skipped = 0
    chat_hash = _hash_chat_id(chat_id)
    logger.info(
        "🎁 [CARDS] 📦 후보 %d개 → 송출 시작 (목표 %d장)",
        len(candidates),
        _MAX_CARDS,
    )
    # 후보 전체를 순회하며 _MAX_CARDS 만큼 성공시까지 시도 (실패 카드는 스킵)
    for c in candidates:
        if sent >= _MAX_CARDS:
            break
        card = _candidate_to_card(c)
        if card is None:
            skipped += 1
            logger.warning(
                "🎁 [CARDS] ⚠️  스킵 (image/url 누락) — %s — %s",
                getattr(c, "brand", "?"),
                (getattr(c, "name", "") or "")[:40],
            )
            continue
        try:
            ok = await adapter.send_card(chat_id, card)
        except Exception:
            logger.exception("🎁 [CARDS] ❌ 송출 예외 → 다음 후보로 chat=%s", chat_hash)
            skipped += 1
            continue
        if not ok:
            skipped += 1
            logger.warning(
                "🎁 [CARDS] ⚠️  Telegram 송출 실패 (이미지 URL 깨짐/타임아웃) → 다음 후보로 — %s — %s",
                getattr(c, "brand", "?"),
                (getattr(c, "name", "") or "")[:40],
            )
            continue
        sent += 1
        logger.info(
            "🎁 [CARDS]   ✅ %d/%d  %s — %s",
            sent,
            _MAX_CARDS,
            getattr(c, "brand", "?"),
            (getattr(c, "name", "") or "")[:50],
        )
    logger.info("🎁 [CARDS] 🏁 완료 sent=%d skipped=%d", sent, skipped)
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


# ────────────────────────────────────────────────────────────────────────────
# State machine: triggers, context, handlers, transition table
# ────────────────────────────────────────────────────────────────────────────


class Trigger(StrEnum):
    """Semantic input classification (state-aware).

    PICK_REQUEST    — picker callback OR digit text in AWAITING_ITEM_PICK
    INTENT_REPLY    — free-text reply while AWAITING_INTENT (no photo)
    NEW_IMAGE_INPUT — photo or URL attached
    TEXT_FALLBACK   — text we don't otherwise route (state-dependent reply)
    """

    PICK_REQUEST = "pick_request"
    INTENT_REPLY = "intent_reply"
    NEW_IMAGE_INPUT = "new_image_input"
    TEXT_FALLBACK = "text_fallback"


@dataclass
class HandlerContext:
    adapter: MessengerAdapter
    session: Session
    message: ChannelMessage
    store: SessionStore
    port: RecommendationPort
    chat_hash: str


Handler = Callable[[HandlerContext], Awaitable[None]]


def classify_input(message: ChannelMessage, session: Session) -> Trigger | None:
    """Map an inbound message (in current session state) to a Trigger.

    Priority order:
        callback ▸ INTENT reply (text in AWAITING_INTENT, no photo) ▸ image ▸
        pick text ▸ generic text.

    The INTENT branch deliberately runs BEFORE the image branch so that a user
    answering the opener with text that happens to contain a URL (e.g.
    "cheaper than https://...") is still treated as intent, matching the
    pre-refactor behavior where the AWAITING_INTENT if-arm came before the
    has_url arm.

    Returns None for "silently ignore".
    """
    if message.callback_data and message.callback_data.startswith("item:"):
        return Trigger.PICK_REQUEST
    if message.text and session.state == SessionState.AWAITING_INTENT and not message.photo_file_id:
        return Trigger.INTENT_REPLY
    if message.photo_file_id or message.urls:
        return Trigger.NEW_IMAGE_INPUT
    if message.text:
        if session.state == SessionState.AWAITING_ITEM_PICK:
            return Trigger.PICK_REQUEST
        return Trigger.TEXT_FALLBACK
    return None


# ── Handlers ────────────────────────────────────────────────────────────────


async def handle_pick_request(ctx: HandlerContext) -> None:
    """Resolve a pick: from callback (item:N) or digit text in AWAITING_ITEM_PICK."""
    msg = ctx.message
    if msg.callback_data and msg.callback_data.startswith("item:"):
        logger.info("🎬 [SCENARIO] ➡️  분기: 아이템 picker 콜백")
        try:
            idx = int(msg.callback_data.split(":", 1)[1])
        except (ValueError, IndexError):
            idx = -1
        if not (0 <= idx < len(ctx.session.detected_items)):
            if msg.callback_query_id and hasattr(ctx.adapter, "answer_callback_query"):
                await ctx.adapter.answer_callback_query(msg.callback_query_id, "Invalid choice")
            return
        if msg.callback_query_id and hasattr(ctx.adapter, "answer_callback_query"):
            await ctx.adapter.answer_callback_query(msg.callback_query_id, None)
        await _select_item(ctx.adapter, ctx.session, idx, ctx.chat_hash, msg.callback_query_id, store=ctx.store)
        return

    # AWAITING_ITEM_PICK + text → digit-based selection
    digit_match = next((c for c in (msg.text or "") if c.isdigit()), None)
    if digit_match is not None:
        idx = int(digit_match) - 1
        if 0 <= idx < len(ctx.session.detected_items):
            logger.info("🎬 [SCENARIO] ➡️  분기: 텍스트로 아이템 선택 idx=%d", idx)
            await _select_item(ctx.adapter, ctx.session, idx, ctx.chat_hash, callback_query_id=None, store=ctx.store)
            return

    logger.info("🎬 [SCENARIO] ⚠️  유효한 번호 아님 → 재안내")
    await ctx.adapter.send_text(ctx.session.chat_id, PICK_INVALID)


async def handle_intent_reply(ctx: HandlerContext) -> None:
    # Cap stored intent length to bound session memory / Redis key size in
    # the future. Downstream query in PipelineRecommendationPort applies a
    # further 256-char cap before dispatch.
    ctx.session.user_intent = (ctx.message.text or "").strip()[:_MAX_INTENT_LEN]
    ctx.session.state = SessionState.SEARCHING
    ctx.store.update(ctx.session)
    logger.info(
        "🎬 [SCENARIO] ➡️  분기: 의도 수집 완료 → 검색 시작 intent_len=%d",
        len(ctx.session.user_intent),
    )
    await _run_search(ctx.adapter, ctx.session, ctx.chat_hash, port=ctx.port, store=ctx.store)


async def handle_new_image(ctx: HandlerContext) -> None:
    msg = ctx.message
    has_photo = bool(msg.photo_file_id)
    has_url = bool(msg.urls)
    logger.info("🎬 [SCENARIO] ➡️  분기: 새 이미지 플로우 시작 (photo=%s url=%s)", has_photo, has_url)

    ctx.session.state = SessionState.LINK_RESOLUTION if (has_url and not has_photo) else SessionState.VISION_PROCESSING
    ctx.session.image_url = None
    ctx.session.detected_items = []
    ctx.session.selected_item_index = None
    ctx.session.vision_keywords = []
    ctx.session.vision_item = None
    ctx.session.user_intent = None
    ctx.store.update(ctx.session)

    try:
        await ctx.adapter.send_chat_action(ctx.session.chat_id, "typing")
    except Exception:
        pass

    image = await _resolve_image_for_message(msg, ctx.adapter)
    if image is None:
        logger.warning("🎬 [SCENARIO] ❌ 이미지 확보 실패 → 거절 멘트")
        await ctx.adapter.send_text(ctx.session.chat_id, LINK_FAIL)
        ctx.session.state = SessionState.IDLE
        ctx.store.update(ctx.session)
        return

    if isinstance(image, str):
        ctx.session.image_url = image
        logger.info("🖼️  [IMAGE] ✅ URL 확보 → vision 단계로 url=%s", image[:120])
    else:
        logger.info("🖼️  [IMAGE] ✅ bytes 확보 (%d B) → vision 단계로", len(image))

    ctx.session.state = SessionState.VISION_PROCESSING
    ctx.store.update(ctx.session)
    try:
        await ctx.adapter.send_chat_action(ctx.session.chat_id, "typing")
    except Exception:
        pass

    vision_data = await vision.extract(image)
    items = vision_data.get("items") or []
    if not items:
        logger.warning("🎬 [SCENARIO] ❌ vision이 아이템을 못 찾음 → 거절 멘트")
        await ctx.adapter.send_text(ctx.session.chat_id, ZERO_RESULT)
        ctx.session.state = SessionState.IDLE
        ctx.store.update(ctx.session)
        return

    ctx.session.detected_items = items

    if len(items) == 1:
        logger.info("🎯 [PICK] 단일 아이템 → picker 생략 label=%s", items[0].get("label"))
        await _select_item(ctx.adapter, ctx.session, 0, ctx.chat_hash, callback_query_id=None, store=ctx.store)
        return

    logger.info("🎯 [PICK] 📤 멀티 아이템 picker 송출 (%d개)", len(items))
    await _send_picker(ctx.adapter, ctx.session.chat_id, items)
    ctx.session.state = SessionState.AWAITING_ITEM_PICK
    ctx.store.update(ctx.session)


async def handle_text_fallback(ctx: HandlerContext) -> None:
    """Text we don't otherwise route — nudge user for a photo when in IDLE/RESULTS_SENT."""
    if ctx.session.state in (SessionState.IDLE, SessionState.RESULTS_SENT):
        await ctx.adapter.send_text(ctx.session.chat_id, TEXT_ONLY)
        return
    logger.info("scenario noop chat=%s state=%s", ctx.chat_hash, ctx.session.state.value)


# ── Transition table ────────────────────────────────────────────────────────
# Key: (state, trigger). state=None means wildcard (any state).
# Lookup falls back from (state, trigger) → (None, trigger).

TRANSITIONS: dict[tuple[SessionState | None, Trigger], Handler] = {
    (None, Trigger.PICK_REQUEST): handle_pick_request,
    (None, Trigger.NEW_IMAGE_INPUT): handle_new_image,
    (SessionState.AWAITING_INTENT, Trigger.INTENT_REPLY): handle_intent_reply,
    (None, Trigger.TEXT_FALLBACK): handle_text_fallback,
}


def _resolve_handler(state: SessionState, trigger: Trigger) -> Handler | None:
    return TRANSITIONS.get((state, trigger)) or TRANSITIONS.get((None, trigger))


# ── Dispatcher ──────────────────────────────────────────────────────────────


@observe(name="scenario_handle")
async def handle(adapter: MessengerAdapter, message: ChannelMessage) -> None:
    chat_id = message.chat_id
    chat_hash = _hash_chat_id(chat_id)
    store = get_store()
    lock = store.lock_for(chat_id)
    async with lock:
        session = store.get_or_create(chat_id)
        logger.info(
            "📥 [INBOUND] chat=%s state=%s text=%r photo=%s urls=%d cb=%s",
            chat_hash,
            session.state.value,
            (message.text or "")[:60],
            "✅" if message.photo_file_id else "❌",
            len(message.urls),
            message.callback_data or "—",
        )

        trigger = classify_input(message, session)
        if trigger is None:
            logger.info("scenario noop chat=%s state=%s (no trigger)", chat_hash, session.state.value)
            return

        handler = _resolve_handler(session.state, trigger)
        if handler is None:
            logger.info(
                "scenario noop chat=%s state=%s trigger=%s (no handler)",
                chat_hash,
                session.state.value,
                trigger.value,
            )
            return

        ctx = HandlerContext(
            adapter=adapter,
            session=session,
            message=message,
            store=store,
            port=get_port(),
            chat_hash=chat_hash,
        )
        await handler(ctx)


# ────────────────────────────────────────────────────────────────────────────
# Helpers (stateless, called from handlers)
# ────────────────────────────────────────────────────────────────────────────


async def _send_picker(adapter: MessengerAdapter, chat_id: int, items: list[dict]) -> None:
    n = len(items)
    lines = []
    for i, it in enumerate(items[:4]):
        num_em = NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}."
        label = it.get("label") or "item"
        desc = it.get("description") or ""
        if desc:
            lines.append(PICKER_LINE.format(num=num_em, label=label, desc=desc))
        else:
            lines.append(f"{num_em}  {label}")
    body = PICKER_HEADER.format(n=n, s="" if n == 1 else "s", lines="\n".join(lines))
    buttons = [(NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}", f"item:{i}") for i in range(min(n, 4))]
    if hasattr(adapter, "send_text_with_buttons"):
        await adapter.send_text_with_buttons(chat_id, body, buttons)
    else:
        await adapter.send_text(chat_id, body)


async def _select_item(
    adapter: MessengerAdapter,
    session: Session,
    idx: int,
    chat_hash: str,
    callback_query_id: str | None,
    store: SessionStore | None = None,
) -> None:
    store = store or get_store()
    chat_id = session.chat_id
    item = session.detected_items[idx]
    session.selected_item_index = idx
    session.vision_item = item.get("label") or "item"
    session.vision_keywords = list(item.get("keywords") or [])
    session.state = SessionState.AWAITING_INTENT
    store.update(session)
    logger.info(
        "🎯 [PICK] ✅ 아이템 선택됨 idx=%d label=%s keywords=%s",
        idx,
        session.vision_item,
        session.vision_keywords,
    )
    logger.info("💬 [OPENER] 📤 의도 질문 송출")
    await adapter.send_text(chat_id, OPENER_TMPL.format(item=session.vision_item))


async def _run_search(
    adapter: MessengerAdapter,
    session: Session,
    chat_hash: str,
    port: RecommendationPort | None = None,
    store: SessionStore | None = None,
) -> None:
    chat_id = session.chat_id
    store = store or get_store()
    port = port or get_port()

    if not session.image_url:
        logger.warning("🔍 [SEARCH] ❌ 이미지 URL 없음 (bytes만 있음) → 검색 불가 chat=%s", chat_hash)
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return

    vision_data = {
        "item": session.vision_item or "item",
        "color": "",
        "keywords": session.vision_keywords,
    }
    channel_req = _build_channel_request(session.image_url, session.user_intent, vision_data)

    logger.info(
        "🔍 [SEARCH] 🚀 파이프라인 호출 item=%s keywords=%s intent_len=%d",
        channel_req.item_label,
        channel_req.keywords,
        len(channel_req.intent or ""),
    )
    t0 = time.perf_counter()
    try:
        result = await port.recommend(channel_req)
    except ValidationError:
        logger.exception("🔍 [SEARCH] ❌ Recommend 요청 빌드 실패 chat=%s", chat_hash)
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return
    except Exception:
        logger.exception("🔍 [SEARCH] ❌ 파이프라인 실패 chat=%s", chat_hash)
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    candidates = result.candidates
    counts = result.counts
    logger.info(
        "🔍 [SEARCH] ✅ 완료 elapsed=%dms 결과=%d (counts: %s)",
        elapsed_ms,
        len(candidates),
        ", ".join(f"{k}={v}" for k, v in (counts or {}).items()) or "—",
    )

    if not candidates:
        logger.warning("🔍 [SEARCH] ⚠️  결과 0개 → 거절 멘트")
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
        logger.info("🎁 [CARDS] ⚠️  최소치 미달 sent=%d (min=%d)", sent, _MIN_CARDS)

    logger.info("💬 [CLOSER] 📤 마무리 멘트 송출")
    await adapter.send_text(chat_id, CLOSER)
    logger.info("🏁 [DONE] 시나리오 1회전 완료 chat=%s sent=%d", chat_hash, sent)
    session.state = SessionState.RESULTS_SENT
    store.update(session)
