"""SPEC-AGENT-001 / REQ-AGENT-004 (node 8/10) — send_results.

Renders result cards via the channel adapter. Caches `last_results` and
accumulates `shown_product_ids` in the SessionStore (REQ-COMPAT-006/007).

Lifts the card-render logic from `scenario._send_results` /
`_candidate_to_card`. No semantic change — same captions, same critique
button rows.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.channels.lang import session_lang
from app.channels.schemas import BotCard
from app.channels.session import SessionState, get_store
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.state import WorkingState
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

_MAX_CARDS = 5
CARD_RENDER_FAIL_EN = "Found some matches but couldn't render the cards — here are the links:"
CARD_RENDER_FAIL_KO = "괜찮은 후보들을 찾았는데 카드로 못 보여줘서 링크로 드릴게요:"
# Back-compat alias
CARD_RENDER_FAIL = CARD_RENDER_FAIL_EN
CRIT_MORE_EN = "♥ More like this"
CRIT_LESS_EN = "✕ Less like this"
CRIT_CHEAP_EN = "💰 Cheaper"
CRIT_MORE_KO = "♥ 비슷한 거 더"
CRIT_LESS_KO = "✕ 다른 느낌"
CRIT_CHEAP_KO = "💰 더 저렴"
# SPEC-IMPLICIT-FB-001 / REQ-FB-UX-001 — 4th critique button (click capture)
CRIT_CLICK_KO = "👀 자세히"
CRIT_CLICK_EN = "👀 View"
# Back-compat aliases
CRIT_MORE = CRIT_MORE_EN
CRIT_LESS = CRIT_LESS_EN
CRIT_CHEAP = CRIT_CHEAP_EN


def _critique_buttons_for(idx: int, lang: str = "en", product_id: str | None = None) -> list[tuple[str, str]]:
    from app.channels.implicit_feedback import click_callback_for

    click_cb = click_callback_for(product_id) if product_id else f"crit:click:{idx}"
    if lang == "ko":
        return [
            (CRIT_MORE_KO, f"crit:more:{idx}"),
            (CRIT_LESS_KO, f"crit:less:{idx}"),
            (CRIT_CHEAP_KO, f"crit:cheap:{idx}"),
            (CRIT_CLICK_KO, click_cb),
        ]
    return [
        (CRIT_MORE_EN, f"crit:more:{idx}"),
        (CRIT_LESS_EN, f"crit:less:{idx}"),
        (CRIT_CHEAP_EN, f"crit:cheap:{idx}"),
        (CRIT_CLICK_EN, click_cb),
    ]


def _format_price(price: Any) -> str | None:
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


def _candidate_to_card(c: Any, idx: int, lang: str = "en") -> BotCard | None:
    image_url = getattr(c, "image_url", None)
    product_url = getattr(c, "product_url", None)
    brand = (getattr(c, "brand", "") or "").strip()
    name = (getattr(c, "name", "") or "").strip()
    platform = (getattr(c, "platform", "") or "").strip()
    subcategory = (getattr(c, "subcategory", "") or "").strip()
    price_str = _format_price(getattr(c, "price", None))

    if not image_url or not product_url:
        return None

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

    if lines:
        caption = "\n".join(lines)
    else:
        caption = "추천 아이템" if lang == "ko" else "Recommended"
    if len(caption) > 1024:
        caption = caption[:1020] + "…"

    if lang == "ko":
        button_label = f"🛒  {brand}에서 보기" if brand else "🛒  지금 보러가기  →"
    else:
        button_label = f"🛒  Shop on {brand}" if brand else "🛒  Shop now  →"
    if len(button_label) > 64:
        button_label = "🛒  지금 보러가기  →" if lang == "ko" else "🛒  Shop now  →"

    try:
        return BotCard(
            image_url=image_url,
            caption=caption,
            button_text=button_label,
            button_url=product_url,
            parse_mode="HTML",
            critique_buttons=_critique_buttons_for(idx, lang=lang, product_id=str(getattr(c, "id", "") or "") or None),
        )
    except ValidationError:
        return None


async def _send_text_fallback(adapter, chat_id: int, candidates: list, limit: int = 3, lang: str = "en") -> int:
    """Plain-text fallback when sendPhoto fails for every candidate."""
    sent = 0
    lines = [CARD_RENDER_FAIL_KO if lang == "ko" else CARD_RENDER_FAIL_EN]
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
        logger.exception("[send_results] text fallback failed")
        return 0
    return sent


@observe(name="node.send_results", as_type="span")
async def send_results(state: WorkingState) -> dict:
    breadcrumbs: list[str] = []
    candidates = list(state.candidates)
    if not candidates:
        breadcrumbs.append("send_results: empty candidates (caller should have routed to respond)")
        return {"log_events": breadcrumbs}

    sess = get_store().get_or_create(state.chat_id)
    chat_id = state.chat_id
    lang = session_lang(sess)

    try:
        adapter = get_adapter()
    except RuntimeError as exc:
        # Adapter ContextVar must be bound before send_results runs (R9).
        # An unbound adapter is a programming bug, not a runtime LLM/IO
        # failure — REQ-AGENT-007 covers the latter. Record a breadcrumb
        # and end the node cleanly so the webhook still returns 200.
        logger.error("[send_results] adapter ContextVar not bound: %s", exc)
        breadcrumbs.append(f"send_results_error: {type(exc).__name__}: adapter unbound")
        return {"log_events": breadcrumbs}

    # Build card list (preserves order, drops invalid).
    pairs: list[tuple[Any, Any]] = []  # (candidate, card)
    for c in candidates:
        if len(pairs) >= _MAX_CARDS:
            break
        card = _candidate_to_card(c, idx=len(pairs), lang=lang)
        if card is None:
            continue
        pairs.append((c, card))

    sent_candidates: list = []
    # DEMO_MODE — fire cards in parallel to cut wall-clock from ~13s to ~4s.
    # Telegram may display them slightly out of arrival order; acceptable for demo.
    from app.core.config import settings as _settings

    if _settings.DEMO_MODE and pairs:
        import asyncio

        async def _send_one(c, card):
            try:
                ok = await adapter.send_card(chat_id, card)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[send_results] send_card raised")
                return c, False, f"{type(exc).__name__}: {exc}"[:200]
            return c, ok, None

        results = await asyncio.gather(*(_send_one(c, card) for c, card in pairs))
        for c, ok, err in results:
            if err:
                breadcrumbs.append(f"send_results_error: {err}")
                continue
            if not ok:
                breadcrumbs.append("send_results: send_card returned False (skip)")
                continue
            sent_candidates.append(c)
    else:
        for c, card in pairs:
            try:
                ok = await adapter.send_card(chat_id, card)
            except Exception as exc:  # REQ-AGENT-007 — log + continue
                logger.exception("[send_results] send_card raised")
                breadcrumbs.append(f"send_results_error: {type(exc).__name__}: {exc}"[:200])
                continue
            if not ok:
                breadcrumbs.append("send_results: send_card returned False (skip)")
                continue
            sent_candidates.append(c)

    sent = len(sent_candidates)

    # 0-card fallback to text list (preserves PR #10's behavior).
    if sent == 0:
        text_sent = await _send_text_fallback(adapter, chat_id, candidates, lang=lang)
        if text_sent > 0:
            sent_candidates = candidates[:text_sent]
            sent = text_sent
            breadcrumbs.append(f"send_results: photo fallback → text list n={sent}")

    if sent == 0:
        return {"log_events": breadcrumbs + ["send_results: nothing dispatched"]}

    # Update session — last_results + accumulate shown_product_ids.
    sess.last_results = list(sent_candidates)
    new_ids = [str(getattr(c, "id", "")) for c in sent_candidates if getattr(c, "id", None)]
    sess.shown_product_ids = list(dict.fromkeys(sess.shown_product_ids + new_ids))
    sess.state = SessionState.RESULTS_SENT
    get_store().update(sess)

    # SPEC-IMPLICIT-FB-001 / REQ-FB-IMPRESSION-001 — log impressions AFTER session persist.
    try:
        from app.channels.implicit_feedback import log_impressions

        await log_impressions(chat_id, sess.from_user_id, sent_candidates)
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail webhook
        logger.warning("[send_results] log_impressions failed: %r", exc)

    breadcrumbs.append(f"send_results: sent={sent}")
    return {
        "sent_candidates": sent_candidates,
        "log_events": breadcrumbs,
    }
