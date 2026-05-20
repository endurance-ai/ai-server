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

import html
import logging
from typing import Any

from app.agents.tool_registry import RespondResult
from app.observability.conversation_log import emit


def _esc(s: Any) -> str:
    """HTML-escape (including quotes) for parse_mode=HTML messages.

    Local (stdlib) — deliberately NOT importing the private
    `send_results._html_escape`; that cross-module private coupling would
    break with a silent ImportError if send_results is ever renamed.
    `quote=True` matters for the `<a href="...">` attribute.
    """
    return html.escape(str(s), quote=True)


def _safe_http_url(url: str) -> str | None:
    """Return the url only if it is an http(s) scheme, else None — prevents a
    catalog-sourced `javascript:` / `data:` URL becoming a tappable link in a
    Telegram HTML-mode message."""
    return url if url.startswith(("https://", "http://")) else None


logger = logging.getLogger(__name__)

# Hybrid delivery: ONE album of up to this many photos (single Telegram
# sendMediaGroup bubble) + ONE summary text message. Telegram caps a media
# group at 10; 5 keeps the album scannable and matches the V1 send_results cap.
_ALBUM_SIZE = 5

# `card_sent` conversation-log event ordinal (event_payloads.py catalog #13).
# The ReAct agent path has no V1-style per-node turn ordering, so this is a
# fixed sentinel for the card-delivery step rather than a derived turn index.
_CARD_SENT_TURN_NO = 9

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


# SPEC-IMPLICIT-FB-001 / REQ-FB-IMPRESSION-001 — per-chat set of product_ids
# already impression-logged this process lifetime. `ai.card_impression` has NO
# unique constraint / ON CONFLICT (migration 0002 — only a NON-unique
# (chat_id, product_id) index), so the same item re-shown via `cards:more`
# (offset paging) or a defensive re-entry would INSERT a duplicate row,
# inflating no-click attribution. Dedupe HERE, at the single delivery seam.
# Module-global, same lifecycle/rationale as `_CARD_BATCH_CURSOR`: not
# persisted, a process restart simply re-allows logging (harmless — at worst
# one extra impression row per item after a restart, bounded by the search cap).
# Memory bound: the inner set is CLEARED at the start of every new search
# (offset==0 fresh-recommendation delivery), so each set stays ≤ the search cap
# (tiny). The outer dict grows by distinct chat_id only — accepted, identical
# structure/precedent to the pre-existing `_CARD_BATCH_CURSOR`; deliberately NO
# LRU/cap (would be over-engineering for the same bound the codebase already
# tolerates).
_LOGGED_IMPRESSION_IDS: dict[int, set[str]] = {}


def reset_card_batch_cursor_for_tests() -> None:
    """Clear the pager cursor + impression-dedup set (test hook only)."""
    _CARD_BATCH_CURSOR.clear()
    _LOGGED_IMPRESSION_IDS.clear()


# @MX:ANCHOR: [AUTO] live-path impression logging — the ONLY invocation of
#   log_impressions on the permanent ReAct topology (send_results is unregistered).
# @MX:REASON: P0 implicit-feedback → Langfuse score depends entirely on this
#   call binding the recommendation trace at delivery time; a regression here
#   silently disables all click / no_click / re_query scoring.
# @MX:SPEC: SPEC-IMPLICIT-FB-001
async def _log_delivered_impressions(chat_id: int, sess: Any, batch: list[Any], *, is_fresh_search: bool) -> None:
    """Impression-log the candidates actually delivered in THIS batch.

    Called from `send_hybrid_batch`'s success seam so it runs inside the
    active Langfuse trace (post-search reply → `node.agent` @observe;
    `cards:more` → `node.ingest` @observe) — `log_impressions` captures that
    trace id and binds it to each row for later click/no-click scoring.

    `is_fresh_search` (the post-search reply, offset==0) CLEARS this chat's
    dedupe set BEFORE logging so a new search re-logs even products that were
    shown in a PREVIOUS search — each gets a fresh impression row bound to the
    NEW trace (otherwise a click on a re-recommended product would attribute to
    the stale prior trace, defeating the feature). A `cards:more` page
    (offset is None → is_fresh_search=False) does NOT clear, so within-search
    paging still dedupes (no double-log of the same result set).

    Offset/batch aware: only `batch` (the slice just sent) is logged. Dedupes
    by (chat_id, product_id) against this search's already-logged set so a
    re-shown item from a later `cards:more` page (or defensive re-entry) is
    not double-inserted (the table has no ON CONFLICT — see migration 0002).

    Strictly fail-open: any failure is swallowed; card delivery already
    succeeded and MUST NOT be undone or delayed by feedback bookkeeping.
    `log_impressions` is itself async + never-raises per its own contract;
    this wrapper is belt-and-suspenders for the dedup/extraction path.
    """
    try:
        from app.channels.implicit_feedback import _product_id_of, log_impressions

        if is_fresh_search:
            # New search → drop the prior search's dedupe set so its products
            # are re-logged against the NEW recommendation trace.
            _LOGGED_IMPRESSION_IDS.pop(int(chat_id), None)
        seen = _LOGGED_IMPRESSION_IDS.setdefault(int(chat_id), set())
        fresh: list[Any] = []
        for c in batch:
            pid = _product_id_of(c)
            if pid is None:
                # Keep id-less candidates (log_impressions skips them itself);
                # they cannot be deduped but also cannot be re-correlated.
                fresh.append(c)
                continue
            if pid in seen:
                continue
            seen.add(pid)
            fresh.append(c)
        if not fresh:
            return
        from_user_id = getattr(sess, "from_user_id", None)
        await log_impressions(int(chat_id), from_user_id, fresh)
    except Exception as exc:  # noqa: BLE001 — never break/delay card delivery
        # type name only — exc repr can surface raw chat_id via psycopg
        # exception args (app/observability/pii.py log-line policy).
        logger.debug("[tool.respond] impression logging best-effort skip: %s", type(exc).__name__)


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
        # Emit `bot_text` to the conversation log so the next turn's memory
        # digest can show the agent what IT just said. Without this the LLM
        # never sees its own questions and treats short replies ("어"/"yes")
        # as fresh fragments instead of answers to its own clarifying prompts
        # (live trace 2026-05-20 18:27). Fail-soft — must never break send.
        try:
            from app.observability.conversation_log import emit

            emit(
                event_type="bot_text",
                user_key=ctx.get("user_key"),
                chat_id=int(chat_id),
                thread_id=ctx.get("thread_id"),
                turn_no=1,
                payload={"chunk_text": text[:1600]},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool.respond] bot_text emit best-effort failed: %r", exc)

        # Pending-question state machine (2026-05-20). When this respond text
        # ends with a question mark we stash the question + the original user
        # intent in an in-process module dict so the NEXT turn's user-message
        # builder (react_loop) can splice the user's short affirmative reply
        # ("어"/"yes") back into the original intent — instead of treating
        # the short reply as a fragmented fresh query.
        #
        # Storage is intentionally NOT on the Session dataclass: the PG-backed
        # session store re-creates dataclasses on every fetch from columns,
        # so any new dataclass field that isn't also in the SQL schema would
        # silently disappear on the next get_or_create. The pending-question
        # module dict survives across the single turn boundary that matters
        # and drops cleanly on restart (which is exactly what we want for
        # ephemeral conversational state). Best-effort — never blocks the send.
        try:
            from app.agents.pending_question import clear_pending, set_pending

            stripped = (text or "").strip()
            is_question = stripped.endswith(("?", "？"))
            if is_question:
                # Original user intent for this turn — best signal is
                # ctx["text_query"] (search_products already overwrites this
                # with the LLM-translated English query). Falls back to None
                # when no search ran this turn; react_loop logs "(unknown)".
                intent = str(ctx.get("text_query") or "").strip() or None
                set_pending(chat_id, stripped, intent)
            else:
                clear_pending(chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[tool.respond] pending_question state best-effort failed: %r", exc)

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
            # A prior (interrupted) entry already delivered text/cards but did
            # not reach the genuine-completion branch — this terminal re-entry
            # is the one that completes the turn. Set trace I/O here too so an
            # interrupted-then-retried turn is NOT left with null trace I/O.
            # `_set_trace_io` is per-turn idempotent (_TRACE_IO_SET_KEY), so a
            # turn that already set it on a prior entry will not double-set.
            # cards_sent==0 in THIS entry — the delivered set is still sourced
            # from sess.last_results inside `_set_trace_io` via _TEXT_SENT/CARD
            # markers signalling a prior delivery; pass the marker-derived flag.
            _set_trace_io(ctx, text, 1 if ctx.get(_SENT_CARD_IDS_KEY) else 0)
            ctx[_DONE_KEY] = True
            return RespondResult(ok=True, error=None, text_sent=False, cards_sent=0)
        # Nothing sent at all (no text, no cards) → empty response.
        return RespondResult(ok=False, error="empty_response", text_sent=False, cards_sent=0)

    # Recommendation turn finalized + delivered. Attach the judge-readable
    # request/result to the root `webhook.telegram` trace so the Langfuse
    # LLM-as-judge has non-null input/output to score. Pure observability,
    # fail-open. Per-turn idempotent (_TRACE_IO_SET_KEY inside _set_trace_io):
    # this is the genuine-completion path, but the partial-delivery re-entry
    # branch above may have already set it — guarded so it fires exactly once
    # per turn on whichever entry completes the turn.
    _set_trace_io(ctx, text, cards_sent)

    ctx[_DONE_KEY] = True
    return RespondResult(ok=True, error=None, text_sent=text_sent, cards_sent=cards_sent)


def _candidate_field(c: Any, key: str, default: Any = "") -> Any:
    v = getattr(c, key, None)
    if v is None and isinstance(c, dict):
        v = c.get(key)
    return default if v is None else v


# Trace-output product cap. The judge scores the recommendation RESULT SET the
# system produced (the diversified candidates), not just the first album page;
# 15 keeps the trace payload compact while covering the full result set.
_TRACE_OUTPUT_MAX = 15
# Bot reply text is the model's OWN generation (not user PII); trimmed so the
# trace `output` stays compact for the judge.
_TRACE_REPLY_MAX = 500
# Vision-attribute cap — bound each string field in the trace `input`.
_TRACE_VISION_MAX = 100
# Per-turn ctx flag: trace I/O was already set this turn. Set inside
# `_set_trace_io`, checked at BOTH call sites so trace I/O fires at most once
# per turn regardless of which entry (interrupted-then-retried) delivered.
_TRACE_IO_SET_KEY = "_respond_trace_io_set"


def _trace_input(ctx: dict[str, Any]) -> dict[str, Any]:
    """Build the judge-readable trace `input` from the request semantics in
    `ctx` — the user's text query and the Vision-derived attributes the
    pipeline actually searched on.

    PII: ctx ALSO carries raw `chat_id` / `from_user_id`; those are
    DELIBERATELY excluded (app/observability/pii.py policy — raw identifiers
    must never enter a trace). The user's own query text is request content,
    consistent with how vision/query already flow through traces; no
    identifiers are added.
    """
    query = str(ctx.get("text_query") or "").strip()
    vision: dict[str, Any] = {}
    cat = ctx.get("vision_category")
    if cat:
        vision["category"] = str(cat)[:_TRACE_VISION_MAX]
    sn = ctx.get("style_node_primary")
    if sn:
        vision["style_node"] = str(sn)[:_TRACE_VISION_MAX]
    payload: dict[str, Any] = {}
    if query:
        payload["query"] = query[:500]
    if vision:
        payload["vision"] = vision
    payload["lang"] = str(ctx.get("lang") or "")
    return payload


def _delivered_products(chat_id: Any) -> list[dict[str, str]]:
    """Top-N {product_id, brand, title} from the recommendation RESULT SET —
    `sess.last_results`, the full diversified result set the system produced
    this turn (the judge should score that, not just the first delivered
    album page of 5). N=`_TRACE_OUTPUT_MAX` (15). Same source
    `send_hybrid_batch` paginates from. Best-effort: returns [] on any
    failure (never raises)."""
    try:
        from app.infrastructure.memory.session import get_store

        store = get_store()
        sess = store.get_or_create(int(chat_id))
        cands = list(getattr(sess, "last_results", None) or [])[:_TRACE_OUTPUT_MAX]
        out: list[dict[str, str]] = []
        for c in cands:
            out.append(
                {
                    "product_id": str(_candidate_field(c, "id", "") or ""),
                    "brand": str(_candidate_field(c, "brand", "") or ""),
                    "title": str(_candidate_field(c, "name", "") or ""),
                }
            )
        return out
    except Exception:  # noqa: BLE001
        return []


# @MX:NOTE: [AUTO] Pure observability — sets the webhook root trace's
#   judge-readable input/output once per recommendation turn. Fail-open:
#   any failure (incl. Langfuse disabled) is swallowed; delivery already
#   happened and MUST NOT be undone or delayed.
def _set_trace_io(ctx: dict[str, Any], text: str, cards_sent: int) -> None:
    """Attach the recommendation turn's `input` (user request) and `output`
    (delivered product set + reply) to the active `webhook.telegram` root
    trace, so the Langfuse LLM-as-judge can score it.

    Idempotent per turn: guarded by `_TRACE_IO_SET_KEY` in the shared `ctx`
    so it fires at most once even if called from both the genuine-completion
    branch AND the partial-delivery re-entry terminal branch (an interrupted-
    then-retried turn must still get trace I/O exactly once, never double).

    Behavior-invariant: no effect on results, ordering, delivery, or latency.
    `update_current_trace` is a no-op when Langfuse is disabled and queues on
    the SDK background thread otherwise (non-blocking). Strictly never raises.
    """
    try:
        if ctx.get(_TRACE_IO_SET_KEY):
            return
        ctx[_TRACE_IO_SET_KEY] = True

        from app.observability import langfuse as _lf

        trace_input = _trace_input(ctx)
        products = _delivered_products(ctx.get("chat_id")) if cards_sent > 0 else []
        trace_output = {
            "products": products,
            "count": len(products),
            "reply": (text or "").strip()[:_TRACE_REPLY_MAX],
        }
        _lf.update_current_trace(input=trace_input, output=trace_output)
    except Exception as exc:  # noqa: BLE001 — pure observability; never break delivery
        logger.debug("[tool.respond] trace I/O set best-effort skip: %s", type(exc).__name__)


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
    n = len(batch)
    if lang == "ko":
        header = f"✨ 마음에 들 만한 {n}개 추려봤어"
    else:
        header = f"✨ Here are {n} picks I think you'll like"
    # Footer help line (KO/EN) — explains the LIKE buttons' purpose so users
    # understand the ❤️ taps are a taste signal, not just "card numbering".
    # The line is rendered AFTER the numbered list (see end of function).
    if lang == "ko":
        help_line = "💡 마음에 든 번호의 ❤️ 를 눌러주면 취향에 반영해서 다음 추천을 더 잘 골라줄게!"
    else:
        help_line = "💡 Tap the ❤️ next to any pick — it tunes my taste model so the next batch fits you better!"
    lines: list[str] = [header, ""]
    for i, c in enumerate(batch, start=1):
        brand = str(_candidate_field(c, "brand", "")).strip()
        name = str(_candidate_field(c, "name", "")).strip() or ("추천 아이템" if lang == "ko" else "item")
        purl = str(_candidate_field(c, "product_url", "")).strip()
        price_str = _format_price(_candidate_field(c, "price", None))
        name_html = _esc(name)
        safe_url = _safe_http_url(purl) if purl else None
        if safe_url:
            name_html = f'<a href="{_esc(safe_url)}">{name_html}</a>'
        bits = [f"{i}."]
        if brand:
            bits.append(f"<b>{_esc(brand)}</b>")
        bits.append(name_html)
        line = " ".join(bits)
        if price_str:
            line += f" · {_esc(price_str)}"
        lines.append(line)
    lines.append("")
    lines.append(help_line)
    return "\n".join(lines)


# Per-item LIKE buttons (1..5). Prefixed with ❤️ so the affordance reads as
# "like this one" rather than "card numbering" (which is what the summary
# list above already does with plain numbers). Paired with the help line in
# `_build_summary` that explains taps feed the taste profile.
_NUM_EMOJI = ["❤️ 1", "❤️ 2", "❤️ 3", "❤️ 4", "❤️ 5"]
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


def _build_keyboard(batch: list[Any], lang: str, has_more: bool = True) -> list[list[tuple[str, str]]]:
    """Row 1: number like-buttons 1️⃣..5️⃣ (card:like). Row 2: footer —
    [더보기/More] (cards:more) ONLY when another batch exists, plus
    [다르게 찾기/Refine] (cards:refine) always."""
    like_row: list[tuple[str, str]] = []
    for i, c in enumerate(batch):
        emoji = _NUM_EMOJI[i] if i < len(_NUM_EMOJI) else str(i + 1)
        like_row.append((emoji, _like_callback_for(c, i)))
    footer: list[tuple[str, str]] = []
    if lang == "ko":
        if has_more:
            footer.append(("➕ 더보기", "cards:more"))
        footer.append(("🔄 다르게 찾기", "cards:refine"))
    else:
        if has_more:
            footer.append(("➕ More", "cards:more"))
        footer.append(("🔄 Refine", "cards:refine"))
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
                turn_no=_CARD_SENT_TURN_NO,
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

    # offset==0 is the post-search reply (fresh recommendation) — see
    # respond.dispatch. offset is None is the `cards:more` pager (subsequent
    # page of the SAME search). This distinguishes a new search (re-log all,
    # bind the new trace) from a within-search page (dedupe so paging doesn't
    # double-log the same result set).
    is_fresh_search = offset == 0
    start = _CARD_BATCH_CURSOR.get(int(chat_id), 0) if offset is None else int(offset)
    if start < 0:
        start = 0
    # Pre-filter to album-eligible candidates from `start` onward, take ≤5.
    # Track each item's ABSOLUTE index in `all_candidates` so the "더보기"
    # cursor advances past the real consumed slice — advancing by the eligible
    # count would skip/repeat items when broken-image candidates are
    # interspersed (P1-2).
    eligible_pos = [(start + i, c) for i, c in enumerate(all_candidates[start:]) if _has_plausible_image(c)]
    batch_pos = eligible_pos[:_ALBUM_SIZE]
    batch = [c for _, c in batch_pos]
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
        keyboard = _build_keyboard(batch, lang, has_more=len(eligible_pos) > len(batch_pos))
        try:
            if hasattr(adapter, "send_text_with_keyboard"):
                await adapter.send_text_with_keyboard(
                    chat_id,
                    summary,
                    keyboard,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
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
        # SPEC-IMPLICIT-FB-001 / REQ-FB-IMPRESSION-001 — log impressions for
        # the candidates ACTUALLY delivered in this batch, AFTER the successful
        # send and BEFORE returning. This is the single live-path seam: both
        # the post-search reply (respond tool, inside node.agent @observe) and
        # the `cards:more` pager (ingest, inside node.ingest @observe) funnel
        # through here, so the active Langfuse trace is captured and bound.
        await _log_delivered_impressions(chat_id, sess, batch[:delivered], is_fresh_search=is_fresh_search)
        # Advance the pager cursor to just past the LAST consumed absolute
        # position in `all_candidates` (not by eligible count) so a "더보기"
        # tap neither repeats nor skips items when broken-image candidates are
        # interspersed (P1-2).
        _CARD_BATCH_CURSOR[int(chat_id)] = (batch_pos[-1][0] + 1) if batch_pos else start + len(batch)
        if ctx is not None:
            ctx[_ALBUM_SENT_KEY] = True
        logger.info(
            "🐱 [respond] hybrid batch delivered n=%d offset=%d fallback=%s",
            delivered,
            start,
            used_fallback,
        )
    return delivered
