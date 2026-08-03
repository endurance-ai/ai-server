"""Telegram webhook endpoint — verifies secret token, normalizes payload,
invokes the LangGraph fashion bot.

SPEC-AGENT-001 (REQ-MIGR-004): replaces the prior call to
`app.channels.scenario.handle(...)` with `await GRAPH.ainvoke(...)`. Channel
adapter, secret-token verification, HTTP 200/401 contract, and parse error
handling are all preserved (REQ-COMPAT-009 / SPEC-MSG-001 REQ-MSG-001/002).

SPEC-CONVERSATION-LOG-001 (LOG-T08/T09/T10): adds intake-time event emission
(`user_text` / `user_photo` / `user_callback`) and 30-day callback thread_id
correlation against `ai.log_conversation_event.card_sent` rows.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from fastapi.responses import ORJSONResponse

from app.channels.adapter import MessengerAdapter
from app.channels.cap_rejection import is_rejection
from app.channels.factory import get_adapter
from app.channels.lang import detect_lang
from app.channels.schemas import ChannelMessage, ChannelParseError
from app.channels.telegram.webhook import verify_secret_token
from app.core.config import settings
from app.graphs.fashion_bot import GRAPH
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import InputState
from app.infrastructure.cache import chat_state, token_cap
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit
from app.observability.event_payloads import CapReachedPayload
from app.observability.langfuse import build_callback_handler, observe, update_current_trace
from app.observability.pii import hash_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/telegram", tags=["webhooks"])


# @MX:ANCHOR: [AUTO] SPEC-CONVERSATION-LOG-001 / REQ-LOG-THREAD-CALLBACK-001
# @MX:REASON: callback thread_id correlation — wrong index → cross-user leak; sole entry point
# @MX:SPEC: SPEC-CONVERSATION-LOG-001
async def _resolve_thread_id(
    *,
    chat_id: int,
    user_key: str,
    source_message_id: int | None,
) -> tuple[UUID, int]:
    """Look up the prior `card_sent` row that produced `source_message_id` and
    return its `(thread_id, turn_no+1)` so callbacks stay on the same thread.

    REQ-LOG-THREAD-CALLBACK-001 — 30-day window, indexed lookup via
    `idx_log_conv_user_time` (user_key + created_at DESC) + GIN over `payload`.

    Cross-user isolation: `user_key = %s` is mandatory. A callback from user A
    must NEVER find user B's card_sent row even if `source_message_id` collides
    across users (Telegram message_ids are per-chat, not globally unique).

    Failure mode: any exception (no pool, transient DB error, missing row,
    stale > 30 days) → `(uuid4(), 0)` fresh seed. NEVER raises.
    """
    # Fast path: no source_message_id means we have nothing to correlate.
    if source_message_id is None:
        return uuid4(), 0

    try:
        from app.providers.db_pool import get_pool

        pool = get_pool()
    except Exception:  # noqa: BLE001 — pool absent (test env / probe failed)
        return uuid4(), 0

    # @MX:NOTE: 쿼리는 `payload @> %s::jsonb` 컨테인먼트로 GIN(jsonb_ops) 인덱스
    # idx_log_conv_payload_gin 을 활용 (이전 `payload->>'source_message_id' = %s`
    # 텍스트 캐스트 형태는 GIN 을 못 타서 seq scan 위험이 있었음 — code review P0-3
    # / security P1-04). user_key + chat_id 등치 + 30일 윈도우로 후보를 좁힌
    # 다음 GIN 컨테인먼트로 source_message_id 일치 판별.
    sql = """
        SELECT thread_id, turn_no
        FROM ai.log_conversation_event
        WHERE user_key = %s
          AND chat_id = %s
          AND event_type = 'card_sent'
          AND created_at > now() - interval '30 days'
          AND payload @> %s::jsonb
        ORDER BY created_at DESC
        LIMIT 1
    """
    from psycopg.types.json import Jsonb

    needle = Jsonb({"source_message_id": int(source_message_id)})
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (user_key, chat_id, needle))
            row = await cur.fetchone()
    except Exception:  # noqa: BLE001 — REQ-LOG-FAILSOFT-001
        return uuid4(), 0

    if row is None:
        return uuid4(), 0
    thread_id, prior_turn_no = row[0], row[1]
    # Defensive: malformed types fall back rather than crash the webhook.
    if not isinstance(thread_id, UUID):
        try:
            thread_id = UUID(str(thread_id))
        except Exception:  # noqa: BLE001
            return uuid4(), 0
    prior_turn_no = int(prior_turn_no) if prior_turn_no is not None else 0
    return thread_id, prior_turn_no + 1


def _extract_callback_source_message_id(payload: Any) -> int | None:
    """Pull `callback_query.message.message_id` from the raw Telegram Update.

    `ChannelMessage` does not surface this field (callback_query_id is the
    inline-button identifier, not the source message). For thread correlation
    we need the message_id of the card the user tapped on.
    """
    if not isinstance(payload, dict):
        return None
    cbq = payload.get("callback_query")
    if not isinstance(cbq, dict):
        return None
    msg = cbq.get("message")
    if not isinstance(msg, dict):
        return None
    mid = msg.get("message_id")
    return mid if isinstance(mid, int) else None


# @MX:NOTE: [AUTO] SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-ENTRY-001 — logging
# clarity helper. Returns the canonical command token (e.g. "/start", "/reset")
# when the inbound text matches a slash command, else None. Dispatch happens
# downstream in the routing layer; this is intake-side logging only.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
def _is_command_text(text: str | None) -> str | None:
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("/") and len(stripped) <= 64:
        # Take the head up to first whitespace to keep payloads bounded.
        head = stripped.split(None, 1)[0]
        return head.lower() if head.startswith("/") else None
    return None


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
        "📥 [webhook] inbound update_id=%s user=%s text=%r photo=%s urls=%s cb=%r",
        update_id,
        hash_id(message.from_user_id),
        (message.text or "")[:80],
        bool(message.photo_file_id),
        [str(u) for u in message.urls],
        (message.callback_data or "")[:64],
    )

    # SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-ENTRY-001 — log slash commands at intake
    # for tracing entry-flow decisions; downstream routing handles the dispatch.
    command = _is_command_text(message.text)
    if command:
        logger.info("📥 [webhook] 🎟 command=%s user=%s", command, hash_id(message.from_user_id))

    # SPEC-CONVERSATION-LOG-001 / LOG-T08+T09+T10 — intake-time emit.
    # Emit FIRST, then schedule the graph. Even if the graph crashes downstream
    # the intake event is captured (REQ-LOG-EMIT-EVERY-NODE-001 floor).
    thread_id, turn_no = await _emit_intake_and_resolve_thread(message, payload)

    background_tasks.add_task(_run_graph_safe, adapter, message, thread_id, turn_no)
    return ORJSONResponse({"ok": True})


async def _emit_intake_and_resolve_thread(
    message: ChannelMessage,
    raw_payload: Any,
) -> tuple[UUID, int]:
    """Resolve the per-update `thread_id` and emit the matching intake event.

    Returns the `(thread_id, turn_no)` to thread into the graph's `InputState`.
    For non-callback updates: fresh `uuid4()` + `turn_no=0`. For callback
    updates: looked up via `_resolve_thread_id` (REQ-LOG-THREAD-CALLBACK-001),
    with `turn_no` = prior_card_sent.turn_no + 1.
    """
    user_key = user_key_for(message.from_user_id, message.chat_id)
    flow = _classify_flow(message)

    if flow == "callback":
        source_message_id = _extract_callback_source_message_id(raw_payload)
        thread_id, turn_no = await _resolve_thread_id(
            chat_id=message.chat_id,
            user_key=user_key,
            source_message_id=source_message_id,
        )
        emit(
            event_type="user_callback",
            user_key=user_key,
            chat_id=message.chat_id,
            thread_id=thread_id,
            turn_no=turn_no,
            payload={
                "callback_data": message.callback_data or "",
                "source_message_id": source_message_id,
            },
        )
        return thread_id, turn_no

    # Non-callback: text or photo. Always seed a fresh thread.
    thread_id = uuid4()
    turn_no = 0
    if flow == "image":
        emit(
            event_type="user_photo",
            user_key=user_key,
            chat_id=message.chat_id,
            thread_id=thread_id,
            turn_no=turn_no,
            payload={
                "attachment_id": message.photo_file_id or "",
                "image_url": str(message.urls[0]) if message.urls else None,
                "caption": message.text,
            },
        )
    else:
        emit(
            event_type="user_text",
            user_key=user_key,
            chat_id=message.chat_id,
            thread_id=thread_id,
            turn_no=turn_no,
            payload={
                "text": message.text or "",
                "lang_detected": detect_lang(message.text),
            },
        )
    return thread_id, turn_no


# SPEC-DAILY-TOKEN-CAP-001 — cap-reached UX (260611 v2): 2 message boxes.
# Box 1: 한도 도달 알림 (자정 리셋 안내).
# Box 2: 멤버십 랜딩 페이지 URL 버튼. 클릭 시 `/m/membership` redirect proxy
# → emit `membership_click` 이벤트 → 302 to MEMBERSHIP_LANDING_URL. URL 버튼
# 이라 텔레그램 자체로는 클릭 데이터가 안 오지만 redirect proxy 가 가운데
# 끼어서 클릭 카운트 + chat_id_hash 캡처 가능. 베타 목표 3 (WTP / 결제
# 전환률) 정량 신호.
_CAP_BOX1: dict[str, str] = {
    "ko": "앗, 오늘 골라줄 수 있는 양을 다 썼어 🐱 자정 지나면 다시 채워지니까 내일 또 와줘!",
    "en": "Whoops — I've used up today's picks 🐱 The quota refills after midnight, so come back tomorrow!",
}
_CAP_BOX2: dict[str, str] = {
    "ko": "근데 기다리기 아쉽지? 월 7,900원이면 하루 종일 같이 디깅할 수 있어 ✨",
    "en": "Hate the wait? ₩7,900/month lets you keep digging all day with kiko ✨",
}
_CAP_MEMBERSHIP_LABEL: dict[str, str] = {
    "ko": "멤버십 보러가기 →",
    "en": "See membership →",
}

# Short reminder used when the user keeps messaging within the 6h cooldown
# after the full 2-box prompt was already sent (option A — anti-fatigue).
_CAP_SHORT_REMINDER: dict[str, str] = {
    "ko": "오늘은 여기까지 🐱 자정 지나면 다시 채워줄게!",
    "en": "That's it for today 🐱 I'll refill after midnight!",
}

# One-time ack when the user explicitly rejects the membership prompt
# ("안 써", "관심 없어", "no thanks", ...) — flips chat into 24h silence
# (option B). Kept warm so it doesn't feel like a slammed door.
_CAP_REJECT_ACK: dict[str, str] = {
    "ko": "알겠어, 더 안 보챌게 🐱 내일 또 보자!",
    "en": "Got it — I'll stop nudging 🐱 See you tomorrow!",
}

# Welcome ack on first return after a cap-hit (fires once when user is back
# under the limit; gated by `kiko:cap_seen:*` 36h flag).
_CAP_WELCOME_BACK: dict[str, str] = {
    "ko": "다시 왔구나 ✨",
    "en": "You're back ✨",
}


@observe(name="webhook.telegram", as_type="span")
async def _invoke_graph(
    adapter: MessengerAdapter,
    message: ChannelMessage,
    thread_id: UUID,
    turn_no: int,
) -> None:
    """Single graph invocation — wraps a root Langfuse trace per webhook.

    REQ-OBS-METADATA-001 — every webhook root trace carries `lang`, `flow`,
    `chat_id_hash`, and (on completion) `critique_retry_count`.

    SPEC-CONVERSATION-LOG-001 — `thread_id` / `turn_no` originate from the
    webhook intake (callback path resolves them against prior `card_sent` rows;
    non-callback path seeds them fresh). Per-node emits read `state.thread_id`
    / `state.turn_no` to maintain a single conversation thread across Updates.
    """
    from app.observability.langfuse import reset_trace_story
    from app.observability.turn_cost import clear_turn, reset_turn

    turn_id = f"{thread_id}:{turn_no}"
    reset_trace_story()
    reset_turn(
        turn_id=turn_id,
        user_key=user_key_for(message.from_user_id, message.chat_id),
        chat_id=message.chat_id,
        thread_id=thread_id,
        turn_no=turn_no,
    )
    token = set_adapter(adapter)
    try:
        # SPEC-DAILY-TOKEN-CAP-001 — gate before invoking the graph.
        # Callback taps (like / more / cap:membership_interest) are cheap UI
        # actions — skip the cap check so they always reach the graph for
        # inline handling (e.g. ingest absorbs the membership tap as a WTP
        # signal even when the user is over-cap).
        if message.callback_data is None and await token_cap.is_over_limit(message.chat_id):
            lang = detect_lang(message.text)
            chat_id = message.chat_id

            # Option B: explicit rejection ("안 써", "관심 없어", ...) — flip
            # 24h silence + one warm ack, then exit. Checked BEFORE silence
            # gate so a fresh rejection refreshes the TTL.
            if is_rejection(message.text):
                ack = _CAP_REJECT_ACK.get(lang, _CAP_REJECT_ACK["en"])
                await chat_state.mark_cap_silenced(chat_id)
                await chat_state.mark_cap_seen(chat_id)
                logger.info(
                    "🚫 [webhook] cap rejection detected chat=%s lang=%s — 24h silence",
                    hash_id(chat_id),
                    lang,
                )
                try:
                    await adapter.send_text(chat_id, ack)
                except Exception:  # noqa: BLE001 — fail-open
                    logger.debug("[webhook] cap reject ack send failed chat=%s", chat_id)
                emit(
                    event_type="cap_reached",
                    user_key=user_key_for(message.from_user_id, chat_id),
                    chat_id=chat_id,
                    thread_id=thread_id,
                    turn_no=turn_no,
                    payload=CapReachedPayload(lang=lang),
                )
                return

            # Option B: already silenced within 24h — no response at all.
            # Conversation-log event still emitted for analytics; UI stays quiet.
            if await chat_state.is_cap_silenced(chat_id):
                await chat_state.mark_cap_seen(chat_id)
                logger.debug("[webhook] cap silenced chat=%s — skip send", hash_id(chat_id))
                emit(
                    event_type="cap_reached",
                    user_key=user_key_for(message.from_user_id, chat_id),
                    chat_id=chat_id,
                    thread_id=thread_id,
                    turn_no=turn_no,
                    payload=CapReachedPayload(lang=lang),
                )
                return

            # Option A: full cap-box already shown within 6h — send only the
            # short reminder. Prevents the 2-box prompt from spamming on every
            # subsequent message while still acknowledging the user.
            if await chat_state.is_cap_shown(chat_id):
                await chat_state.mark_cap_seen(chat_id)
                short = _CAP_SHORT_REMINDER.get(lang, _CAP_SHORT_REMINDER["en"])
                logger.debug("[webhook] cap cooldown active chat=%s — short reminder", hash_id(chat_id))
                try:
                    await adapter.send_text(chat_id, short)
                except Exception:  # noqa: BLE001 — fail-open
                    logger.debug("[webhook] cap short reminder send failed chat=%s", chat_id)
                emit(
                    event_type="cap_reached",
                    user_key=user_key_for(message.from_user_id, chat_id),
                    chat_id=chat_id,
                    thread_id=thread_id,
                    turn_no=turn_no,
                    payload=CapReachedPayload(lang=lang),
                )
                return

            # First cap hit (or post-cooldown re-hit) → full 2-box flow.
            box1 = _CAP_BOX1.get(lang, _CAP_BOX1["en"])
            box2 = _CAP_BOX2.get(lang, _CAP_BOX2["en"])
            membership_label = _CAP_MEMBERSHIP_LABEL.get(lang, _CAP_MEMBERSHIP_LABEL["en"])
            logger.info(
                "🚫 [webhook] token cap exceeded chat=%s lang=%s",
                hash_id(chat_id),
                lang,
            )
            # Box 1 — quota notice (plain text).
            try:
                await adapter.send_text(chat_id, box1)
            except Exception:  # noqa: BLE001 — fail-open
                logger.debug("[webhook] cap box1 send failed chat=%s", chat_id)
            # Box 2 — membership URL button. Routes through `/m/membership`
            # redirect proxy so clicks emit `membership_click` events
            # (Telegram won't notify us about raw URL button taps directly).
            from app.core.config import settings as _settings_for_cap

            base = (_settings_for_cap.PUBLIC_BASE_URL or "").rstrip("/")
            if base:
                chat_hash = hash_id(chat_id) or ""
                membership_url = f"{base}/m/membership?source=cap&c={chat_hash}"
            else:
                # PUBLIC_BASE_URL unset → fall back to direct landing (no
                # click-count tracking but the link still works).
                membership_url = _settings_for_cap.MEMBERSHIP_LANDING_URL or "https://kikoai.me/price"
            try:
                if hasattr(adapter, "send_text_with_url_button"):
                    await adapter.send_text_with_url_button(
                        chat_id,
                        box2,
                        membership_label,
                        membership_url,
                    )
                else:
                    # Adapter lacks URL-button helper → fall back to plain text
                    # with the URL inline (still tappable in Telegram).
                    await adapter.send_text(
                        chat_id,
                        f"{box2}\n\n{membership_label} {membership_url}",
                    )
            except Exception:  # noqa: BLE001 — fail-open
                logger.debug("[webhook] cap box2 send failed chat=%s", chat_id)
            await chat_state.mark_cap_shown(chat_id)
            await chat_state.mark_cap_seen(chat_id)
            emit(
                event_type="cap_reached",
                user_key=user_key_for(message.from_user_id, chat_id),
                chat_id=chat_id,
                thread_id=thread_id,
                turn_no=turn_no,
                payload=CapReachedPayload(lang=lang),
            )
            return

        # Under-limit return path: if this chat hit the cap within the last
        # 36h, fire a one-shot welcome-back ack and clear the flag. Skip for
        # callback taps (UI actions are not a "fresh return"). Fail-open —
        # welcome is best-effort; main graph proceeds regardless.
        if message.callback_data is None and await chat_state.is_cap_seen(message.chat_id):
            welcome_lang = detect_lang(message.text)
            welcome = _CAP_WELCOME_BACK.get(welcome_lang, _CAP_WELCOME_BACK["en"])
            try:
                await adapter.send_text(message.chat_id, welcome)
            except Exception:  # noqa: BLE001 — fail-open
                logger.debug("[webhook] cap welcome send failed chat=%s", message.chat_id)
            await chat_state.clear_cap_seen(message.chat_id)

        input_state = InputState(
            message=message,
            chat_id=message.chat_id,
            from_user_id=message.from_user_id,
            thread_id=thread_id,
            turn_no=turn_no,
        )
        session_id = hash_id(message.chat_id)
        user_id = hash_id(message.from_user_id)
        flow = _classify_flow(message)
        # REQ-OBS-METADATA-001 — attach root-trace metadata + identity. `name`
        # is set explicitly so the trace title never shows blank in the Langfuse
        # UI even when CallbackHandler nesting is misaligned. `session_id` /
        # `user_id` bind the trace to the user without leaking raw identifiers
        # (pre-hashed via pii.hash_id). `input` records the inbound signal for
        # ALL turn types (callback-only / Q&A / image) so every trace has
        # non-null input; recommendation turns overwrite this with the richer
        # search-query payload via `_set_trace_io` in the `respond` tool.
        raw_input: dict = {"flow": flow}
        if message.text:
            raw_input["text"] = message.text[:200]
        if message.photo_file_id:
            raw_input["has_photo"] = True
        if message.callback_data:
            raw_input["callback"] = message.callback_data[:64]
        update_current_trace(
            name="webhook.telegram",
            session_id=session_id,
            user_id=user_id,
            input=raw_input,
            metadata={
                "flow": flow,
                "chat_id_hash": session_id,
                "channel": "telegram",
                "graph": "fashion_bot",
                "turn_id": turn_id,
            },
        )
        handler = build_callback_handler(
            session_id=session_id,
            user_id=user_id,
            metadata={
                "channel": "telegram",
                "graph": "fashion_bot",
                "flow": flow,
                "turn_id": turn_id,
            },
        )
        callbacks = [handler] if handler is not None else []
        await GRAPH.ainvoke(input_state, config={"callbacks": callbacks})
    finally:
        reset_adapter(token)
        clear_turn()
        reset_trace_story()


async def _run_graph_safe(
    adapter: MessengerAdapter,
    message: ChannelMessage,
    thread_id: UUID,
    turn_no: int,
) -> None:
    try:
        await _invoke_graph(adapter, message, thread_id, turn_no)
    except Exception:
        logger.exception("📥 [webhook] ❌ fashion_bot graph background task failed")
