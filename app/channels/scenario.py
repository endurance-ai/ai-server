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

import asyncio
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
from app.channels.critique import CritiqueDelta
from app.channels.critique import parse_callback as parse_critique_callback
from app.channels.recommendation import (
    ChannelRecommendationRequest,
    RecommendationPort,
    get_port,
)
from app.channels.router import RoutedDecision, RoutedIntent, TasteUpdate, route_text
from app.channels.schemas import BotCard, ChannelMessage
from app.channels.session import Session, SessionState, SessionStore, get_store
from app.channels.taste_profile import (
    TasteProfile,
    TasteProfileStore,
    get_taste_store,
    user_key_for,
)
from app.core.config import settings
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

PICKER_HEADER = "I see {n} item{s} in this photo 👀\n\n{lines}\n\nWhich one are you after? Tap below 👇"
PICKER_LINE = "{num}  {label} — {desc}"
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
OPENER_TMPL = "Got it — {item} 👌\nSame vibe, something cheaper, or a specific color?"
SEARCH_START_TMPL = "On it — hunting for {item}-style picks 🔎"
REFINE_START = "Reading you — refining the picks 🔁"
ZERO_RESULT = "Hmm, I couldn't find a match — try another angle or a different photo."
CARD_RENDER_FAIL = "Found some matches but couldn't render the cards — here are the links:"
CLOSER = "Tap any to see more like it ✨"
CLOSER_REFINE = "Tweak it more, or send a new photo whenever 👌"
LINK_FAIL = "Sorry, couldn't load that. Try sharing the photo directly."
PHOTO_DIRECT_NOT_SUPPORTED = (
    "I can't search direct photo uploads yet 🙏\n"
    "Try sharing a Pinterest / image link instead 📌\n"
    "(Direct upload support coming soon!)"
)
TEXT_ONLY = "Send me a photo or a Pinterest link first 📸"
PICK_INVALID = "Tap one of the buttons above to choose an item 👆"
TYPING_HEARTBEAT_INTERVAL = 4.0  # Telegram chat action stays for ~5s

# Critique / taste copy
CRIT_MORE = "♥ More like this"
CRIT_LESS = "✕ Less like this"
CRIT_CHEAP = "💰 Cheaper"
CRIT_NOOP = "Got it — looking at the same recipe but with your tweak ✨"
TASTE_ACK_TMPL = "Noted: {summary} 📝"
TASTE_ACK_EMPTY = "Noted 📝"
NEW_SEARCH_NEEDS_IMAGE = "Sounds good — share a photo or a Pinterest link to start 📸"
PRESEARCH_REFINE_TMPL = "Refining: {summary} 🔁"

_MAX_CARDS = 5
_MIN_CARDS = 4
_MAX_INTENT_LEN = 512  # session storage cap; downstream query is further trimmed to 256 in PipelineRecommendationPort


def _hash_chat_id(chat_id: int) -> str:
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def _build_channel_request(
    image_url: str,
    intent: str | None,
    vision_data: dict,
    *,
    delta: CritiqueDelta | None = None,
    taste: TasteProfile | None = None,
    exclude_product_ids: list[str] | None = None,
) -> ChannelRecommendationRequest:
    """Compose the request, layering critique deltas + taste profile on top of
    the base vision signal. Order of precedence on conflicting fields:
    user critique (this turn) > taste profile (long-term) > vision."""
    keywords = list(vision_data.get("keywords") or [])
    color = vision_data.get("color") or None
    intent_combined = intent
    exclude_brands: list[str] = []
    exclude_keywords: list[str] = []
    boost_brands: list[str] = []
    boost_keywords: list[str] = []
    max_price: int | None = None
    min_price: int | None = None

    if delta is not None:
        exclude_brands.extend(delta.exclude_brands)
        exclude_keywords.extend(delta.exclude_keywords)
        boost_keywords.extend(delta.boost_keywords)
        max_price = delta.max_price
        min_price = delta.min_price
        if delta.color:
            color = delta.color
        if delta.extra_intent:
            extra = delta.extra_intent.strip()
            base = (intent or "").strip()
            # Avoid duplication when handlers stored the raw text in both
            # session.user_intent and delta.extra_intent (router fallback path).
            if not base:
                intent_combined = extra
            elif extra.lower() == base.lower() or extra.lower() in base.lower():
                intent_combined = base
            else:
                intent_combined = f"{base} {extra}"

    if taste is not None:
        boost_brands.extend(taste.boost_brands(top_n=5))
        boost_keywords.extend(taste.boost_keywords(top_n=5))
        # Taste exclude_brands are STRONG — same as delta-supplied. Liked
        # brands win on conflict because we appended deltas first.
        exclude_brands.extend(b for b in taste.exclude_brands(threshold=1.5) if b not in boost_brands)

    return ChannelRecommendationRequest(
        image_url=image_url,
        item_label=vision_data.get("item"),
        intent=intent_combined,
        keywords=keywords,
        tolerance=0.5,
        color=color,
        exclude_brands=list(dict.fromkeys(b.strip().lower() for b in exclude_brands if b and b.strip())),
        exclude_keywords=list(dict.fromkeys(k.strip().lower() for k in exclude_keywords if k and k.strip())),
        exclude_product_ids=list(exclude_product_ids or []),
        boost_brands=list(dict.fromkeys(b.strip().lower() for b in boost_brands if b and b.strip())),
        boost_keywords=list(dict.fromkeys(k.strip().lower() for k in boost_keywords if k and k.strip())),
        max_price=max_price,
        min_price=min_price,
    )


def _critique_buttons_for(idx: int) -> list[tuple[str, str]]:
    """Inline-keyboard row attached to each result card.
    callback_data format: crit:{op}:{idx}  (op ∈ more|less|cheap)."""
    return [
        (CRIT_MORE, f"crit:more:{idx}"),
        (CRIT_LESS, f"crit:less:{idx}"),
        (CRIT_CHEAP, f"crit:cheap:{idx}"),
    ]


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


def _candidate_to_card(c: Any, idx: int = 0, with_critique: bool = True) -> BotCard | None:
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
            critique_buttons=_critique_buttons_for(idx) if with_critique else [],
        )
    except ValidationError:
        return None


async def _send_results(adapter: MessengerAdapter, chat_id: int, candidates: list) -> list:
    """Returns the list of *successfully sent* candidates in send order, so
    the caller can persist them for callback-resolution + exclude-on-refine."""
    sent_candidates: list = []
    skipped = 0
    chat_hash = _hash_chat_id(chat_id)
    logger.info(
        "🎁 [CARDS] 📦 후보 %d개 → 송출 시작 (목표 %d장)",
        len(candidates),
        _MAX_CARDS,
    )
    # idx is the *display* position (0..MAX_CARDS-1) — what the user sees as
    # "card 1, card 2..." and what callback_data references.
    for c in candidates:
        if len(sent_candidates) >= _MAX_CARDS:
            break
        idx = len(sent_candidates)
        card = _candidate_to_card(c, idx=idx, with_critique=True)
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
        sent_candidates.append(c)
        logger.info(
            "🎁 [CARDS]   ✅ %d/%d  %s — %s",
            len(sent_candidates),
            _MAX_CARDS,
            getattr(c, "brand", "?"),
            (getattr(c, "name", "") or "")[:50],
        )
    logger.info("🎁 [CARDS] 🏁 완료 sent=%d skipped=%d", len(sent_candidates), skipped)
    return sent_candidates


def _is_vision_fallback(items: list[dict]) -> bool:
    """vision.py returns a single placeholder item ({label:'item', keywords:[]})
    when the LLM call fails or returns malformed JSON. Detect that shape so
    handle_new_image can short-circuit instead of pretending we identified
    something."""
    if len(items) != 1:
        return False
    only = items[0]
    label = (only.get("label") or "").strip().lower()
    keywords = only.get("keywords") or []
    return label == "item" and not keywords


async def _typing_heartbeat(adapter: MessengerAdapter, chat_id: int) -> None:
    """Keep the 'typing' indicator alive while a long-running step (search) is
    in flight. Telegram clears the indicator after ~5s, so we re-send every
    TYPING_HEARTBEAT_INTERVAL until cancelled."""
    try:
        while True:
            try:
                await adapter.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            await asyncio.sleep(TYPING_HEARTBEAT_INTERVAL)
    except asyncio.CancelledError:
        return


async def _send_text_card_fallback(adapter: MessengerAdapter, chat_id: int, candidates: list, limit: int = 3) -> int:
    """Plain-text fallback when sendPhoto fails for every candidate (broken
    image hosts, hotlink protection). Lists product name + brand + URL so the
    user still gets something actionable."""
    sent = 0
    lines = [CARD_RENDER_FAIL]
    for c in candidates:
        if sent >= limit:
            break
        product_url = getattr(c, "product_url", None)
        if not product_url:
            continue
        brand = (getattr(c, "brand", "") or "").strip()
        name = (getattr(c, "name", "") or "").strip() or "item"
        price_str = _format_price(getattr(c, "price", None))
        bits = [f"• {name}"]
        if brand:
            bits.append(f"({brand})")
        if price_str:
            bits.append(f"— {price_str}")
        bits.append(f"\n  {product_url}")
        lines.append(" ".join(bits))
        sent += 1
    if sent == 0:
        return 0
    try:
        await adapter.send_text(chat_id, "\n\n".join(lines))
    except Exception:
        logger.exception("🎁 [CARDS] ❌ 텍스트 fallback 송출 실패")
        return 0
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
    CRITIQUE_TAP    — inline-keyboard callback (crit:{op}:{idx}) on a result card
    REFINE_REQUEST  — free-text reply while RESULTS_SENT (no photo). Now
                      LLM-routed inside handler — could resolve to refine,
                      taste-update, new-search-request, or off-topic.
    NEW_IMAGE_INPUT — photo or URL attached
    TEXT_FALLBACK   — text we don't otherwise route (state-dependent reply)
    """

    PICK_REQUEST = "pick_request"
    INTENT_REPLY = "intent_reply"
    CRITIQUE_TAP = "critique_tap"
    REFINE_REQUEST = "refine_request"
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
    taste_store: TasteProfileStore | None = None  # None ⇒ taste profile disabled

    def taste(self) -> TasteProfile | None:
        if self.taste_store is None:
            return None
        return self.taste_store.get_or_create(user_key_for(self.session.from_user_id, self.session.chat_id))


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
    if message.callback_data and message.callback_data.startswith("crit:"):
        return Trigger.CRITIQUE_TAP
    if message.text and session.state == SessionState.AWAITING_INTENT and not message.photo_file_id:
        return Trigger.INTENT_REPLY
    # RESULTS_SENT + text (no photo, no url) → ambiguous (refine vs taste-update
    # vs new-search vs off-topic). Routed via LLM inside the handler.
    if message.text and session.state == SessionState.RESULTS_SENT and not message.photo_file_id and not message.urls:
        return Trigger.REFINE_REQUEST
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

    logger.info("🎬 [SCENARIO] ⚠️  유효한 번호 아님 → picker 재송출")
    # Re-send the picker so users who scrolled past the buttons get fresh ones,
    # instead of just nudging "tap above 👆" into the void.
    if ctx.session.detected_items:
        await _send_picker(ctx.adapter, ctx.session.chat_id, ctx.session.detected_items)
    else:
        await ctx.adapter.send_text(ctx.session.chat_id, PICK_INVALID)


async def handle_intent_reply(ctx: HandlerContext) -> None:
    # Cap stored intent length to bound session memory / Redis key size in
    # the future. Downstream query in PipelineRecommendationPort applies a
    # further 256-char cap before dispatch.
    ctx.session.user_intent = (ctx.message.text or "").strip()[:_MAX_INTENT_LEN]
    ctx.session.state = SessionState.SEARCHING
    # Reset shown_product_ids on a fresh-intent search — the user is committing
    # to a new query, so it's fine if previous "shown" items resurface.
    ctx.session.shown_product_ids = []
    ctx.store.update(ctx.session)
    logger.info(
        "🎬 [SCENARIO] ➡️  분기: 의도 수집 완료 → 검색 시작 intent_len=%d",
        len(ctx.session.user_intent),
    )
    await _run_search(
        ctx.adapter,
        ctx.session,
        ctx.chat_hash,
        port=ctx.port,
        store=ctx.store,
        taste=ctx.taste(),
    )


def _summarize_delta(delta: CritiqueDelta) -> str:
    """One-line natural-language summary of a CritiqueDelta for the pre-search
    confirmation message. Deterministic, no LLM."""
    bits: list[str] = []
    if delta.exclude_keywords:
        bits.append("less " + ", ".join(delta.exclude_keywords[:2]))
    if delta.exclude_brands:
        bits.append("not " + ", ".join(delta.exclude_brands[:2]))
    if delta.boost_keywords:
        bits.append("more " + ", ".join(delta.boost_keywords[:2]))
    if delta.color:
        bits.append(f"in {delta.color}")
    if delta.max_price is not None:
        bits.append(f"under ₩{delta.max_price:,}")
    if delta.min_price is not None:
        bits.append(f"over ₩{delta.min_price:,}")
    if not bits and delta.extra_intent:
        bits.append(delta.extra_intent[:60])
    return ", ".join(bits) if bits else "your tweak"


def _summarize_taste_update(update: TasteUpdate) -> str:
    bits: list[str] = []
    if update.liked_brands:
        bits.append("you like " + ", ".join(update.liked_brands[:3]))
    if update.disliked_brands:
        bits.append("you avoid " + ", ".join(update.disliked_brands[:3]))
    if update.liked_keywords:
        bits.append("you like " + ", ".join(update.liked_keywords[:3]))
    if update.disliked_keywords:
        bits.append("you avoid " + ", ".join(update.disliked_keywords[:3]))
    return "; ".join(bits)


def _apply_taste_update(profile: TasteProfile | None, update: TasteUpdate) -> None:
    if profile is None:
        return
    for b in update.liked_brands:
        profile.reinforce_liked_brand(b, weight=2.0)  # explicit > implicit
    for b in update.disliked_brands:
        profile.reinforce_disliked_brand(b, weight=2.0)
    if update.liked_keywords:
        profile.reinforce_liked_keywords(update.liked_keywords, weight=2.0)
    if update.disliked_keywords:
        profile.reinforce_disliked_keywords(update.disliked_keywords, weight=2.0)


async def handle_refine_request(ctx: HandlerContext) -> None:
    """RESULTS_SENT + free text → route via LLM (router) into one of:
        - critique_text   → apply CritiqueDelta + re-search
        - taste_update    → update TasteProfile, ack
        - new_search_req  → ask user for an image
        - off_topic       → soft nudge

    Guards: when image_url / vision_item are missing (TTL eviction), critique
    paths still need the old context to make sense — fall back to TEXT_ONLY."""
    text = (ctx.message.text or "").strip()
    if not text:
        return

    decision: RoutedDecision = await route_text(text, ctx.session.state, ctx.session.last_results)
    logger.info("🎬 [SCENARIO] ➡️  refine 라우팅 결과 intent=%s", decision.intent.value)

    if decision.intent == RoutedIntent.TASTE_UPDATE and decision.taste_update is not None:
        profile = ctx.taste()
        _apply_taste_update(profile, decision.taste_update)
        if profile is not None and ctx.taste_store is not None:
            ctx.taste_store.update(profile)
        summary = _summarize_taste_update(decision.taste_update)
        ack = TASTE_ACK_TMPL.format(summary=summary) if summary else TASTE_ACK_EMPTY
        await ctx.adapter.send_text(ctx.session.chat_id, ack)
        return

    if decision.intent == RoutedIntent.NEW_SEARCH_REQUEST:
        await ctx.adapter.send_text(ctx.session.chat_id, NEW_SEARCH_NEEDS_IMAGE)
        return

    if decision.intent == RoutedIntent.OFF_TOPIC:
        # Don't drag the user into a search they didn't ask for. Stay quiet
        # unless we're confident — soft nudge is enough.
        await ctx.adapter.send_text(ctx.session.chat_id, TEXT_ONLY)
        return

    # CRITIQUE_TEXT — re-search applying the parsed delta.
    if not ctx.session.image_url or not ctx.session.vision_item:
        logger.info("🎬 [SCENARIO] ⚠️  critique 요청이지만 이전 컨텍스트 없음 → 사진 안내")
        await ctx.adapter.send_text(ctx.session.chat_id, TEXT_ONLY)
        return

    delta = decision.critique_delta or CritiqueDelta(op="free_text", extra_intent=text[:200])
    ctx.session.user_intent = text[:_MAX_INTENT_LEN]
    ctx.session.state = SessionState.SEARCHING
    ctx.store.update(ctx.session)
    summary = _summarize_delta(delta)
    await _run_search(
        ctx.adapter,
        ctx.session,
        ctx.chat_hash,
        port=ctx.port,
        store=ctx.store,
        is_refine=True,
        delta=delta,
        taste=ctx.taste(),
        exclude_already_shown=True,
        presearch_summary=summary,
    )


async def handle_critique_tap(ctx: HandlerContext) -> None:
    """Inline-keyboard tap on a result card: crit:{op}:{idx}.
    Resolves the anchor from session.last_results, builds CritiqueDelta,
    reinforces taste profile (more=liked / less=disliked), re-runs search."""
    msg = ctx.message
    cb_id = msg.callback_query_id
    delta = parse_critique_callback(msg.callback_data or "", ctx.session.last_results)
    if delta is None or delta.anchor is None:
        if cb_id and hasattr(ctx.adapter, "answer_callback_query"):
            await ctx.adapter.answer_callback_query(cb_id, "Out of date — try a fresh search")
        return
    if cb_id and hasattr(ctx.adapter, "answer_callback_query"):
        # Friendly toast confirming the tap was received.
        toast = {"more": "Finding more like this ✨", "less": "Steering away ✕", "cheap": "Going cheaper 💰"}.get(
            delta.op, "Got it"
        )
        await ctx.adapter.answer_callback_query(cb_id, toast)

    # Reinforce taste profile based on signal direction.
    profile = ctx.taste()
    if profile is not None and delta.anchor.brand:
        if delta.op == "more":
            profile.reinforce_liked_brand(delta.anchor.brand, weight=1.0)
        elif delta.op == "less":
            profile.reinforce_disliked_brand(delta.anchor.brand, weight=1.0)
        if profile is not None and ctx.taste_store is not None:
            ctx.taste_store.update(profile)

    if not ctx.session.image_url or not ctx.session.vision_item:
        logger.info("🎬 [SCENARIO] ⚠️  critique tap이지만 이전 컨텍스트 없음 → 사진 안내")
        await ctx.adapter.send_text(ctx.session.chat_id, TEXT_ONLY)
        return

    ctx.session.state = SessionState.SEARCHING
    ctx.store.update(ctx.session)
    summary = _summarize_delta(delta)
    await _run_search(
        ctx.adapter,
        ctx.session,
        ctx.chat_hash,
        port=ctx.port,
        store=ctx.store,
        is_refine=True,
        delta=delta,
        taste=ctx.taste(),
        exclude_already_shown=(delta.op != "more"),  # "more" wants the anchor's neighbors, including some shown ones
        presearch_summary=summary,
    )


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

    if isinstance(image, bytes):
        # Direct photo upload: vision works on bytes, but Modal /embed only
        # accepts URLs — searching would silently dead-end at _run_search.
        # Fail fast with a clear message instead of burning vision tokens.
        logger.info("🖼️  [IMAGE] ⛔ 직접 업로드 (%d B) — 검색 불가, 안내 후 종료", len(image))
        await ctx.adapter.send_text(ctx.session.chat_id, PHOTO_DIRECT_NOT_SUPPORTED)
        ctx.session.state = SessionState.IDLE
        ctx.store.update(ctx.session)
        return

    ctx.session.image_url = image
    logger.info("🖼️  [IMAGE] ✅ URL 확보 → vision 단계로 url=%s", image[:120])

    ctx.session.state = SessionState.VISION_PROCESSING
    ctx.store.update(ctx.session)
    try:
        await ctx.adapter.send_chat_action(ctx.session.chat_id, "typing")
    except Exception:
        pass

    vision_data = await vision.extract(image)
    items = vision_data.get("items") or []
    if not items or _is_vision_fallback(items):
        # Either vision returned nothing OR returned its placeholder fallback
        # (LLM call failed / JSON malformed). Treat both as "couldn't read the
        # photo" rather than dragging the user through a doomed picker → intent
        # → search loop.
        logger.warning(
            "🎬 [SCENARIO] ❌ vision 인식 실패(items=%d, fallback=%s) → 거절 멘트",
            len(items),
            _is_vision_fallback(items) if items else False,
        )
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
    (None, Trigger.CRITIQUE_TAP): handle_critique_tap,
    (None, Trigger.NEW_IMAGE_INPUT): handle_new_image,
    (SessionState.AWAITING_INTENT, Trigger.INTENT_REPLY): handle_intent_reply,
    (SessionState.RESULTS_SENT, Trigger.REFINE_REQUEST): handle_refine_request,
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
        # Capture user identity for taste-profile lookup. Falls back gracefully
        # when message has no from_user (channel posts, system events).
        if message.from_user_id and not session.from_user_id:
            session.from_user_id = message.from_user_id
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
            taste_store=get_taste_store() if settings.TASTE_PROFILE_ENABLED else None,
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
    *,
    is_refine: bool = False,
    delta: CritiqueDelta | None = None,
    taste: TasteProfile | None = None,
    exclude_already_shown: bool = False,
    presearch_summary: str | None = None,
) -> None:
    chat_id = session.chat_id
    store = store or get_store()
    port = port or get_port()
    if presearch_summary:
        start_msg = PRESEARCH_REFINE_TMPL.format(summary=presearch_summary)
    else:
        start_msg = REFINE_START if is_refine else SEARCH_START_TMPL.format(item=session.vision_item or "item")
    closer_msg = CLOSER_REFINE if is_refine else CLOSER

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
    excl_ids = list(session.shown_product_ids) if exclude_already_shown else []
    channel_req = _build_channel_request(
        session.image_url,
        session.user_intent,
        vision_data,
        delta=delta,
        taste=taste,
        exclude_product_ids=excl_ids,
    )

    # Surface the search to the user: a one-shot start message + a typing
    # heartbeat that survives the multi-second pipeline call (Telegram clears
    # the typing indicator after ~5s). Without this users see ~5s of silence
    # and assume the bot died.
    try:
        await adapter.send_text(chat_id, start_msg)
    except Exception:
        logger.exception("🔍 [SEARCH] start 메시지 송출 실패 (계속 진행)")
    heartbeat = asyncio.create_task(_typing_heartbeat(adapter, chat_id))

    logger.info(
        "🔍 [SEARCH] 🚀 파이프라인 호출 item=%s keywords=%s intent_len=%d",
        channel_req.item_label,
        channel_req.keywords,
        len(channel_req.intent or ""),
    )
    t0 = time.perf_counter()
    try:
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
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):
            pass
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

    sent_list = await _send_results(adapter, chat_id, candidates)
    sent = len(sent_list)
    if sent == 0:
        # Photo cards all failed (broken image URLs / hotlink protection / 4s
        # sendPhoto timeout). Search itself succeeded — give the user the
        # links as plain text instead of pretending we found nothing.
        text_sent = await _send_text_card_fallback(adapter, chat_id, candidates)
        if text_sent == 0:
            await adapter.send_text(chat_id, ZERO_RESULT)
            session.state = SessionState.IDLE
            store.update(session)
            return
        logger.info("🎁 [CARDS] ↩️  사진 송출 0장 → 텍스트 fallback %d개 송출", text_sent)
        # Text fallback: keep the search candidates so callbacks could still
        # reference them, but no critique buttons were rendered.
        sent_list = candidates[:text_sent]
        sent = text_sent

    if sent < _MIN_CARDS:
        logger.info("🎁 [CARDS] ⚠️  최소치 미달 sent=%d (min=%d)", sent, _MIN_CARDS)

    # Cache last_results for subsequent critique callbacks + the next refine's
    # exclude_product_ids list.
    session.last_results = list(sent_list)
    new_ids = [str(getattr(c, "id", "")) for c in sent_list if getattr(c, "id", None)]
    # Accumulate across refines so we don't keep showing the same products.
    session.shown_product_ids = list(dict.fromkeys(session.shown_product_ids + new_ids))

    logger.info("💬 [CLOSER] 📤 마무리 멘트 송출")
    await adapter.send_text(chat_id, closer_msg)
    logger.info("🏁 [DONE] 시나리오 1회전 완료 chat=%s sent=%d", chat_hash, sent)
    session.state = SessionState.RESULTS_SENT
    store.update(session)
