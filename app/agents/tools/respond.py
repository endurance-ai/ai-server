"""SPEC-AGENT-V2-REACT / T-003g — `respond` tool wrapper.

Sends a natural-language reply (LLM-generated text passed by agent) plus the
REAL product cards from THIS turn's most recent search — sourced internally
from `sess.last_results`, NEVER hand-serialized by the LLM. NO `_Flow` enum —
the agent LLM is the single source of phrasing. Loop-terminating tool:
`terminates_loop=True` in REGISTRY.

Idempotency: `respond` has side effects (Telegram text + card carousel). The
TERMINAL tool is no longer retried by react_loop (SPEC-AGENT-V2-REACT fix —
a partial send + full retry double-sent everything). As belt-and-suspenders
this module is ALSO self-idempotent: the text-sent flag is set in `ctx`
IMMEDIATELY after the single text send (BEFORE the slow card loop), and each
successfully-sent card id is tracked, so any defensive second entry skips the
already-sent text and already-sent cards — the user never sees a duplicate.

@MX:NOTE: [AUTO] Side effect: sends Telegram messages (text + cards).
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import RespondResult
from app.observability.conversation_log import emit

logger = logging.getLogger(__name__)

# Hybrid delivery: ONE album of up to this many photos (single Telegram
# sendMediaGroup bubble) + ONE summary text message. Telegram caps a media
# group at 10; 5 keeps the album scannable and matches the V1 send_results cap.
_ALBUM_SIZE = 5

# Per-turn idempotency keys in the shared ctx dict (built once per turn and
# passed by reference, so they survive a defensive second dispatch).
_DONE_KEY = "_respond_dispatched"
_TEXT_SENT_KEY = "_respond_text_sent"
_SENT_CARD_IDS_KEY = "_respond_sent_card_ids"
# Per-turn flag — the album+summary was already dispatched this turn, so a
# defensive second entry (or react_loop retry) must not resend it.
_ALBUM_SENT_KEY = "_respond_album_sent"

# In-memory "더보기 / More" pager cursor, keyed by chat_id. Module-global so it
# survives across webhook calls within the process (the original search turn
# sets it; a later `cards:more` tap reads + advances it). NOT persisted: a
# process restart simply restarts the pager from the first batch, which is
# harmless (last_results may not survive an in-memory store restart either).
# Avoiding a Session field keeps this change free of an Alembic migration
# (the PG session store uses an explicit column list, not field reflection).
_CARD_BATCH_CURSOR: dict[int, int] = {}


def reset_card_batch_cursor_for_tests() -> None:
    """Clear the pager cursor (test hook only)."""
    _CARD_BATCH_CURSOR.clear()


def _has_plausible_image(c: Any) -> bool:
    """A candidate is album-eligible only with a usable http(s) image URL AND
    a product URL (the exact validity gate `_candidate_to_card` enforces).

    sendMediaGroup is ATOMIC — one bad photo URL fails the whole group — so we
    pre-filter rather than let Telegram reject the batch.
    """
    img = getattr(c, "image_url", None)
    if img is None and isinstance(c, dict):
        img = c.get("image_url")
    purl = getattr(c, "product_url", None)
    if purl is None and isinstance(c, dict):
        purl = c.get("product_url")
    if not purl:
        return False
    s = str(img or "").strip()
    return s.startswith(("http://", "https://"))


def _candidate_identity(c: Any) -> Any:
    """Stable per-card dedup key from the SOURCE candidate (not the rendered
    BotCard, which carries no id). Prefer the product id; fall back to the
    product_url then image_url so dedup still works for id-less candidates.
    Returns None only when nothing identifying is available (then that card
    is not deduped — accepted: missing id is rare and the no-retry fix in
    react_loop already removes the systemic double-send)."""
    pid = getattr(c, "id", None)
    if pid is None and isinstance(c, dict):
        pid = c.get("id")
    if pid:
        return ("id", str(pid))
    for attr in ("product_url", "image_url"):
        v = getattr(c, attr, None)
        if v is None and isinstance(c, dict):
            v = c.get(attr)
        if v:
            return (attr, str(v))
    return None


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> RespondResult:
    text = (args.get("text") or "").strip()
    chat_id = ctx.get("chat_id")
    if chat_id is None:
        return RespondResult(ok=False, error="missing_chat_id", text_sent=False, cards_sent=0)

    # Idempotency fast-path: a fully-completed prior dispatch is a no-op. The
    # terminal tool is no longer retried by react_loop, but a defensive second
    # entry (or a future caller change) must not resend anything.
    if ctx.get(_DONE_KEY):
        logger.debug("[tool.respond] retry after successful dispatch — skipping resend")
        return RespondResult(ok=True, error=None, text_sent=False, cards_sent=0)

    if not text:
        # Pure card turn is not expected (the agent always writes a reply); an
        # empty text with no candidates is an empty response.
        pass

    try:
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
    except Exception as exc:  # noqa: BLE001
        return RespondResult(ok=False, error=f"adapter_missing:{type(exc).__name__}", text_sent=False, cards_sent=0)

    text_sent = False
    cards_sent = 0

    # Send the LLM-generated text first (adapter guards empty/whitespace).
    # Set the text-sent flag IMMEDIATELY after the single send, BEFORE the slow
    # card loop — so a (defensive) second entry never resends the text even if
    # the first entry was interrupted mid-carousel.
    if text and not ctx.get(_TEXT_SENT_KEY):
        try:
            # Cap to safety length (matches v1 respond.py: 4 * RESPONSE_MAX_TOKENS).
            await adapter.send_text(chat_id, text[:1600])
            text_sent = True
            ctx[_TEXT_SENT_KEY] = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[tool.respond] send_text failed: %r", exc)

    # Source cards INTERNALLY from this turn's last search, rendered via the
    # EXACT V1 card path (send_results._candidate_to_card + adapter.send_card).
    # The LLM never provides cards.
    #
    # CRITICAL GATE: send cards ONLY when `search_products` / `refine_search`
    # actually ran AND returned >0 candidates THIS turn — signalled by the
    # per-turn `CARDS_READY_KEY` marker in the shared `ctx` (set by
    # `persist_last_results`). `sess.last_results` PERSISTS across turns, so
    # gating on its non-emptiness re-blasted the previous search's cards onto
    # every greeting / chit-chat / clarify turn. Gating on the per-turn marker
    # fixes that: no search this turn (or 0 results) → marker unset → text
    # only, zero cards. `sess.last_results` is intentionally NOT cleared (V1
    # critique callbacks still rely on it); only this SEND GATE changed.
    from app.agents.tools.search_products import CARDS_READY_KEY

    if ctx.get(CARDS_READY_KEY):
        cards_sent = await send_hybrid_batch(adapter, chat_id, ctx=ctx, offset=0)

    if not text_sent and cards_sent == 0:
        # Nothing sent THIS entry. If a prior (interrupted) entry already
        # delivered the text or some cards, this is a benign idempotent
        # re-entry — report ok, not an empty response.
        if ctx.get(_TEXT_SENT_KEY) or ctx.get(_SENT_CARD_IDS_KEY):
            ctx[_DONE_KEY] = True
            return RespondResult(ok=True, error=None, text_sent=False, cards_sent=0)
        # Nothing sent at all (no text, no cards) → empty response.
        return RespondResult(ok=False, error="empty_response", text_sent=False, cards_sent=0)

    ctx[_DONE_KEY] = True
    return RespondResult(ok=True, error=None, text_sent=text_sent, cards_sent=cards_sent)


def _candidate_field(c: Any, key: str, default: Any = "") -> Any:
    v = getattr(c, key, None)
    if v is None and isinstance(c, dict):
        v = c.get(key)
    return default if v is None else v


def _format_price(price: Any) -> str | None:
    """Mirror `send_results._format_price` (₩-grouped, drops non-positive)."""
    if price is None:
        return None
    try:
        n = int(price)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"₩{n:,}"


def _build_summary(batch: list[Any], lang: str) -> str:
    """KO/EN header + numbered list. The item NAME is an HTML <a> link to the
    product URL (parse_mode HTML) so the buy path survives without per-card
    Shop buttons (Telegram media groups forbid per-photo keyboards).
    """
    from app.graphs.nodes.send_results import _html_escape

    n = len(batch)
    if lang == "ko":
        header = f"✨ 마음에 들 만한 {n}개 추려봤어요"
    else:
        header = f"✨ Here are {n} picks I think you'll like"
    lines: list[str] = [header, ""]
    for i, c in enumerate(batch, start=1):
        brand = str(_candidate_field(c, "brand", "")).strip()
        name = str(_candidate_field(c, "name", "")).strip() or ("추천 아이템" if lang == "ko" else "item")
        purl = str(_candidate_field(c, "product_url", "")).strip()
        price_str = _format_price(_candidate_field(c, "price", None))
        name_html = _html_escape(name)
        if purl:
            name_html = f'<a href="{_html_escape(purl)}">{name_html}</a>'
        bits = [f"{i}"]
        if brand:
            bits.append(f"<b>{_html_escape(brand)}</b>")
        bits.append(name_html)
        line = " ".join(bits)
        if price_str:
            line += f" · {_html_escape(price_str)}"
        lines.append(line)
    return "\n".join(lines)


# Number emoji for the per-item like buttons (1..5).
_NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
# Telegram callback_data 64-byte budget minus the "card:like:" prefix (10).
_LIKE_SUFFIX_BUDGET = 53


def _like_callback_for(c: Any, idx: int) -> str:
    """`card:like:{product_id_or_idx}` — product id truncated to the last
    _LIKE_SUFFIX_BUDGET chars (same scheme as implicit_feedback.click_callback_for
    so `resolve_click_target` suffix-matching keeps working). Falls back to the
    batch index when the candidate has no id."""
    pid = str(_candidate_field(c, "id", "") or "").strip()
    if not pid:
        return f"card:like:{idx}"
    if len(pid) > _LIKE_SUFFIX_BUDGET:
        pid = pid[-_LIKE_SUFFIX_BUDGET:]
    return f"card:like:{pid}"


def _build_keyboard(batch: list[Any], lang: str) -> list[list[tuple[str, str]]]:
    """Row 1: number like-buttons 1️⃣..5️⃣ (card:like). Row 2: footer
    [더보기/More] (cards:more) + [다르게 찾기/Refine] (cards:refine)."""
    like_row: list[tuple[str, str]] = []
    for i, c in enumerate(batch):
        emoji = _NUM_EMOJI[i] if i < len(_NUM_EMOJI) else str(i + 1)
        like_row.append((emoji, _like_callback_for(c, i)))
    if lang == "ko":
        footer = [("➕ 더보기", "cards:more"), ("🔄 다르게 찾기", "cards:refine")]
    else:
        footer = [("➕ More", "cards:more"), ("🔄 Refine", "cards:refine")]
    return [like_row, footer]


async def _fallback_send_cards(
    adapter: Any,
    chat_id: int,
    batch: list[Any],
    sent_ids: set,
    lang: str,
) -> int:
    """Per-card `send_card` loop — the V1 path, used as the safety net when
    `send_media_group` is unavailable or fails atomically (so a search NEVER
    yields zero cards). Reuses `send_results._candidate_to_card` verbatim."""
    if not hasattr(adapter, "send_card"):
        return 0
    from app.graphs.nodes.send_results import _candidate_to_card

    sent = 0
    for c in batch:
        ident = _candidate_identity(c)
        if ident is not None and ident in sent_ids:
            continue
        card = _candidate_to_card(c, idx=sent, lang=lang)
        if card is None:
            continue
        try:
            mid = await adapter.send_card(chat_id, card)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool.respond] fallback send_card failed: %r", exc)
            continue
        if mid is None:
            continue
        if ident is not None:
            sent_ids.add(ident)
        sent += 1
    return sent


def _emit_card_sent(chat_id: int, sess: Any, batch: list[Any]) -> None:
    """Emit one `card_sent` (catalog #13) per delivered item — preserves the
    V1 send_results per-item emission so observability stays equivalent."""
    try:
        from app.infrastructure.memory.taste_profile import user_key_for

        user_key = user_key_for(getattr(sess, "from_user_id", None), chat_id)
        for pos, c in enumerate(batch):
            emit(
                event_type="card_sent",
                user_key=user_key,
                chat_id=chat_id,
                thread_id=None,
                turn_no=9,
                payload={
                    "product_id": str(_candidate_field(c, "id", "") or ""),
                    "position": pos,
                    "send_ok": True,
                },
            )
    except Exception:  # noqa: BLE001 — emit must never break delivery
        logger.debug("[tool.respond] card_sent emit best-effort")


# @MX:ANCHOR: [AUTO] hybrid result delivery — fan_in from respond tool +
#   ingest cards:more handler; the single album/summary renderer.
# @MX:REASON: both the post-search reply path and the "더보기" pager funnel
#   through here, so its album/summary/idempotency contract is load-bearing.
async def send_hybrid_batch(
    adapter: Any,
    chat_id: int,
    *,
    ctx: dict[str, Any] | None = None,
    offset: int | None = None,
) -> int:
    """Send ONE album (sendMediaGroup, ≤5 photos, one bubble) + ONE summary
    text message (numbered HTML-linked list + inline keyboard) for the slice
    of `sess.last_results` starting at `offset`.

    `offset=None` (the "더보기" path) reads the in-memory pager cursor for this
    chat. The post-search reply passes `offset=0` and a per-turn `ctx` so the
    album+summary is idempotent across a defensive react_loop re-entry.

    Robustness: pre-filters to candidates with a plausible image URL, caps the
    album at 5, and FALLS BACK to the per-card `send_card` loop when
    `send_media_group` is unavailable or fails atomically — a search never
    yields zero cards. Returns the number of cards delivered (0 on no
    candidates / nothing left / total failure handled by the fallback).
    """
    try:
        from app.channels.lang import session_lang
        from app.infrastructure.memory.session import get_store

        store = get_store()
        sess = store.get_or_create(int(chat_id))
        all_candidates = list(getattr(sess, "last_results", None) or [])
        if not all_candidates:
            return 0
        lang = session_lang(sess)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool.respond] last_results lookup failed: %r", exc)
        return 0

    # Idempotency: a defensive second entry within the SAME turn must not
    # resend the album+summary (the slow path the no-retry fix targets).
    if ctx is not None and ctx.get(_ALBUM_SENT_KEY):
        logger.debug("[tool.respond] album already sent this turn — skipping resend")
        return 0

    start = _CARD_BATCH_CURSOR.get(int(chat_id), 0) if offset is None else int(offset)
    if start < 0:
        start = 0
    # Pre-filter to album-eligible candidates from `start` onward, take ≤5.
    eligible = [c for c in all_candidates[start:] if _has_plausible_image(c)]
    batch = eligible[:_ALBUM_SIZE]
    if not batch:
        logger.debug("[tool.respond] no album-eligible candidates from offset=%d", start)
        return 0

    sent_ids: set = ctx.setdefault(_SENT_CARD_IDS_KEY, set()) if ctx is not None else set()

    delivered = 0
    used_fallback = False
    media = [{"image_url": str(_candidate_field(c, "image_url", "")), "caption": None} for c in batch]
    # Telegram media groups require 2..10 items; a single eligible candidate
    # cannot form a group → go straight to the per-card path for that one.
    can_group = len(media) >= 2 and hasattr(adapter, "send_media_group")
    group_ok = False
    if can_group:
        try:
            group_ok = await adapter.send_media_group(chat_id, media)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool.respond] send_media_group raised: %r", exc)
            group_ok = False

    if group_ok:
        delivered = len(batch)
        for c in batch:
            ident = _candidate_identity(c)
            if ident is not None:
                sent_ids.add(ident)
        # Summary text + inline keyboard (carries the buy links + actions the
        # media group cannot). HTML so the item names are tappable links.
        summary = _build_summary(batch, lang)
        keyboard = _build_keyboard(batch, lang)
        try:
            if hasattr(adapter, "send_text_with_keyboard"):
                await adapter.send_text_with_keyboard(chat_id, summary, keyboard)
            else:
                await adapter.send_text(chat_id, summary)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool.respond] summary send failed: %r", exc)
    else:
        # Album unavailable or atomic-fail → per-card fallback for THIS batch.
        used_fallback = True
        delivered = await _fallback_send_cards(adapter, chat_id, batch, sent_ids, lang)

    if delivered > 0:
        _emit_card_sent(chat_id, sess, batch[:delivered])
        # Advance the pager cursor past this batch's source slice. The slice
        # consumed is `all_candidates[start : start + len(batch_source)]`; we
        # advance by the batch size (eligible items are contiguous enough for
        # a "더보기" pager — a skipped non-eligible item just won't reappear).
        _CARD_BATCH_CURSOR[int(chat_id)] = start + len(batch)
        if ctx is not None:
            ctx[_ALBUM_SENT_KEY] = True
        logger.info(
            "🐱 [respond] hybrid batch delivered n=%d offset=%d fallback=%s",
            delivered,
            start,
            used_fallback,
        )
    return delivered
