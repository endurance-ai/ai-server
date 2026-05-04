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

        # 1) 콜백 — 사용자가 아이템 picker 버튼을 탭함
        if message.callback_data and message.callback_data.startswith("item:"):
            logger.info("🎬 [SCENARIO] ➡️  분기: 아이템 picker 콜백")
            await _handle_item_pick(adapter, session, message, chat_hash)
            return

        # 2) AWAITING_INTENT 상태에서 텍스트 답장 → 검색 시작
        if session.state == SessionState.AWAITING_INTENT and message.text and not message.photo_file_id:
            session.user_intent = message.text.strip()
            session.state = SessionState.SEARCHING
            store.update(session)
            logger.info("🎬 [SCENARIO] ➡️  분기: 의도 수집 완료 → 검색 시작 intent=%r", session.user_intent[:80])
            await _run_search(adapter, session, chat_hash)
            return

        # 3) AWAITING_ITEM_PICK 상태에서 "1" 같은 숫자 텍스트 → 아이템 선택
        if session.state == SessionState.AWAITING_ITEM_PICK and message.text:
            digit_match = next((c for c in message.text if c.isdigit()), None)
            if digit_match is not None:
                idx = int(digit_match) - 1
                if 0 <= idx < len(session.detected_items):
                    logger.info("🎬 [SCENARIO] ➡️  분기: 텍스트로 아이템 선택 idx=%d", idx)
                    await _select_item(adapter, session, idx, chat_hash, callback_query_id=None)
                    return
            logger.info("🎬 [SCENARIO] ⚠️  유효한 번호 아님 → 재안내")
            await adapter.send_text(chat_id, PICK_INVALID)
            return

        # 4) 사진/URL 입력 → 새 이미지 플로우 시작
        has_photo = bool(message.photo_file_id)
        has_url = bool(message.urls)

        if has_photo or has_url:
            logger.info("🎬 [SCENARIO] ➡️  분기: 새 이미지 플로우 시작 (photo=%s url=%s)", has_photo, has_url)
            if has_url and not has_photo:
                session.state = SessionState.LINK_RESOLUTION
            else:
                session.state = SessionState.VISION_PROCESSING
            session.image_url = None
            session.detected_items = []
            session.selected_item_index = None
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
                logger.warning("🎬 [SCENARIO] ❌ 이미지 확보 실패 → 거절 멘트")
                await adapter.send_text(chat_id, LINK_FAIL)
                session.state = SessionState.IDLE
                store.update(session)
                return

            if isinstance(image, str):
                session.image_url = image
                logger.info("🖼️  [IMAGE] ✅ URL 확보 → vision 단계로 url=%s", image[:120])
            else:
                logger.info("🖼️  [IMAGE] ✅ bytes 확보 (%d B) → vision 단계로", len(image))

            session.state = SessionState.VISION_PROCESSING
            store.update(session)
            try:
                await adapter.send_chat_action(chat_id, "typing")
            except Exception:
                pass

            vision_data = await vision.extract(image)
            items = vision_data.get("items") or []
            if not items:
                logger.warning("🎬 [SCENARIO] ❌ vision이 아이템을 못 찾음 → 거절 멘트")
                await adapter.send_text(chat_id, ZERO_RESULT)
                session.state = SessionState.IDLE
                store.update(session)
                return

            session.detected_items = items

            # 아이템 1개 → picker 생략하고 바로 opener
            if len(items) == 1:
                logger.info("🎯 [PICK] 단일 아이템 → picker 생략 label=%s", items[0].get("label"))
                await _select_item(adapter, session, 0, chat_hash, callback_query_id=None)
                return

            # 아이템 여러 개 → picker 송출
            logger.info("🎯 [PICK] 📤 멀티 아이템 picker 송출 (%d개)", len(items))
            await _send_picker(adapter, chat_id, items)
            session.state = SessionState.AWAITING_ITEM_PICK
            store.update(session)
            return

        # Text-only without URL in IDLE (or anywhere else) -> nudge for photo
        if message.text and session.state in (SessionState.IDLE, SessionState.RESULTS_SENT):
            await adapter.send_text(chat_id, TEXT_ONLY)
            return

        # Fallback: ignore silently
        logger.info("scenario noop chat=%s state=%s", chat_hash, session.state.value)


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


async def _handle_item_pick(
    adapter: MessengerAdapter,
    session: Session,
    message: ChannelMessage,
    chat_hash: str,
) -> None:
    try:
        idx = int(message.callback_data.split(":", 1)[1])
    except (ValueError, IndexError):
        idx = -1
    if not (0 <= idx < len(session.detected_items)):
        if message.callback_query_id and hasattr(adapter, "answer_callback_query"):
            await adapter.answer_callback_query(message.callback_query_id, "Invalid choice")
        return
    if message.callback_query_id and hasattr(adapter, "answer_callback_query"):
        await adapter.answer_callback_query(message.callback_query_id, None)
    await _select_item(adapter, session, idx, chat_hash, callback_query_id=message.callback_query_id)


async def _select_item(
    adapter: MessengerAdapter,
    session: Session,
    idx: int,
    chat_hash: str,
    callback_query_id: str | None,
) -> None:
    store = get_store()
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


async def _run_search(adapter: MessengerAdapter, session: Session, chat_hash: str) -> None:
    chat_id = session.chat_id
    store = get_store()

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
    try:
        req = _build_recommend_request(session.image_url, session.user_intent, vision_data)
    except ValidationError:
        logger.exception("🔍 [SEARCH] ❌ RecommendRequest 빌드 실패 chat=%s", chat_hash)
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return

    logger.info(
        "🔍 [SEARCH] 🚀 파이프라인 호출 query=%r item=%s",
        req.item.search_query[:80],
        req.item.category,
    )
    import time as _t

    t0 = _t.perf_counter()
    try:
        resp = await run_pipeline(req)
    except Exception:
        logger.exception("🔍 [SEARCH] ❌ 파이프라인 실패 chat=%s", chat_hash)
        await adapter.send_text(chat_id, ZERO_RESULT)
        session.state = SessionState.IDLE
        store.update(session)
        return
    elapsed_ms = int((_t.perf_counter() - t0) * 1000)

    candidates = list(resp.results) if resp and resp.results else []
    counts = getattr(resp, "counts", {}) if resp else {}
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
