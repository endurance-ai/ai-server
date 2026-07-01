"""Consumer chat service — bridges UUID-based user identity to the LangGraph fashion bot.

Strategy: CaptureAdapter (batch) / StreamingAdapter (SSE)
  The existing graph sends responses via MessengerAdapter.send_text / send_card.
  CaptureAdapter collects responses in-process for batch return.
  StreamingAdapter puts events into an asyncio.Queue for SSE streaming.

User identity bridge:
  The graph uses `chat_id: int` for session/taste-profile lookups.
  Consumer users have `user_id: UUID`. We derive a stable int from the UUID bytes
  so the same user always resolves to the same session key in the existing stores.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.channels.adapter import MessengerAdapter
from app.channels.schemas import BotCard, BotReply, ChannelMessage
from app.graphs.fashion_bot import GRAPH
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import InputState
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.langfuse import build_callback_handler, observe, update_current_trace
from app.observability.pii import hash_id
from app.observability.turn_cost import clear_turn, reset_turn

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

logger = logging.getLogger(__name__)

_SENTINEL = object()  # signals StreamingAdapter queue is closed
_KST = timezone(timedelta(hours=9))


def _user_id_to_chat_id(user_id: UUID) -> int:
    """Derive a stable positive int from a UUID for graph session key compatibility."""
    return abs(int.from_bytes(user_id.bytes[:8], "big")) % (2**62)


# user_profiles uses ('male','female','other'); taste_profile uses ('men','women','unisex')
_GENDER_MAP = {"male": "men", "female": "women", "other": "unisex"}
_APP_TO_CAP_TIER = {
    "free": "free",
    "basic": "standard",
    "standard": "standard",
    "pro": "pro",
    "premium": "pro",
    "developer": "developer",
}


@dataclass(frozen=True)
class AppCapStatus:
    user_tier: str
    cap_tier: str
    daily_cap: int
    cap_used: int
    cap_remaining: int | None
    cap_reset_at: str
    cap_reached: bool

    @property
    def cap_reset_at_kst(self) -> str:
        return datetime.fromisoformat(self.cap_reset_at).astimezone(_KST).isoformat()

    @property
    def cap_reset_display(self) -> str:
        return datetime.fromisoformat(self.cap_reset_at).astimezone(_KST).strftime("%Y-%m-%d %H:%M KST")

    def session_payload(self) -> dict:
        return {
            "user_tier": self.user_tier,
            "daily_cap": self.daily_cap,
            "cap_used": self.cap_used,
            "cap_remaining": self.cap_remaining,
            "cap_reset_at": self.cap_reset_at,
            "cap_reset_at_kst": self.cap_reset_at_kst,
            "cap_reset_display": self.cap_reset_display,
        }

    def cap_event_payload(self) -> dict:
        return {
            "code": "daily_token_cap_reached",
            "user_tier": self.user_tier,
            "used": self.cap_used,
            "cap": self.daily_cap,
            "remaining": 0,
            "reset_at": self.cap_reset_at,
            "reset_at_kst": self.cap_reset_at_kst,
            "reset_display": self.cap_reset_display,
            "cta": "upgrade",
        }


async def _sync_gender_to_taste_profile(
    pool: AsyncConnectionPool,
    user_id: UUID,
    synthetic_chat_id: int,
) -> None:
    """Pre-populate taste profile gender from ai.user_profiles so the gender-pin
    gate in search_products doesn't block REST API users on first search."""
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT gender FROM ai.user_profiles WHERE user_id = %s",
                (user_id,),
            )
            row = await cur.fetchone()
        db_gender = (row[0] or "").strip().lower() if row else None
        # Default to unisex so the gender-pin gate never blocks REST API users.
        gender_token = _GENDER_MAP.get(db_gender or "", "unisex")
        from app.infrastructure.memory.taste_profile import get_taste_store, user_key_for

        user_key = user_key_for(None, synthetic_chat_id)
        store = get_taste_store()
        profile = store.get_or_create(user_key)
        if not profile.gender:
            profile.gender = gender_token
            store.update(profile)
    except Exception:
        logger.debug("[chat_service] gender sync skipped", exc_info=True)


async def _get_app_user_tier(pool: AsyncConnectionPool, user_id: UUID) -> str:
    """Return the app subscription tier cached on user_profiles."""
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT tier FROM ai.user_profiles WHERE user_id = %s", (user_id,))
            row = await cur.fetchone()
    except Exception:
        logger.debug("[chat_service] tier lookup skipped", exc_info=True)
        return "free"
    tier = str(row[0] or "free").strip().lower() if row else "free"
    return tier or "free"


async def get_app_cap_status(pool: AsyncConnectionPool, user_id: UUID) -> AppCapStatus:
    """Resolve the current app-user tier and daily token cap status."""
    from app.core.config import settings
    from app.infrastructure.cache import token_cap

    user_tier = await _get_app_user_tier(pool, user_id)
    cap_tier = _APP_TO_CAP_TIER.get(user_tier, "free")
    daily_cap = int(token_cap._tier_cap(cap_tier))  # noqa: SLF001 - shared cap SoT
    cap_used = int(await token_cap.get_usage(_user_id_to_chat_id(user_id)))
    cap_remaining = None if daily_cap == 0 else max(0, daily_cap - cap_used)
    cap_reached = bool(settings.DAILY_TOKEN_CAP_ENABLED and daily_cap > 0 and cap_used >= daily_cap)
    return AppCapStatus(
        user_tier=user_tier,
        cap_tier=cap_tier,
        daily_cap=daily_cap,
        cap_used=cap_used,
        cap_remaining=cap_remaining,
        cap_reset_at=token_cap.reset_at_kst_midnight().isoformat(),
        cap_reached=cap_reached,
    )


class CaptureAdapter(MessengerAdapter):
    """Collects graph outputs in-process instead of dispatching to a messenger."""

    def __init__(self) -> None:
        self._texts: list[str] = []
        self._cards: list[BotCard] = []

    async def parse_inbound(self, payload: dict) -> ChannelMessage:
        raise NotImplementedError("CaptureAdapter is send-only")

    async def send_text(self, chat_id: int, text: str) -> None:
        self._texts.append(text)

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:
        self._cards.append(card)
        return 0  # non-None signals success to send_results (no text fallback)

    def get_reply(self) -> BotReply:
        texts = self._texts[:]
        closing = texts[-1] if len(texts) > 1 else None
        main_text = texts[0] if texts else None
        return BotReply(
            text=main_text,
            cards=list(self._cards),
            closing_text=closing if closing != main_text else None,
        )


def _infer_clarify_axis(callback_data: str) -> str:
    """Infer the UI axis from a callback_data prefix — pure string parsing so the
    6 hasattr-fallback call sites (pick_item, ask_clarify, intro, ask_user_clarification,
    search_products, suggest_next_step) never need to pass an explicit axis.

    item:N -> pick_item / clarify:{axis}:val -> {axis} (gender, category_pick, ...)
    onboard:lang:xx -> lang / suggest:... -> suggest_next_step / card(s):... -> cards
    """
    parts = callback_data.split(":", 2)
    head = parts[0] if parts else ""
    if head == "item":
        return "pick_item"
    if head in ("clarify", "onboard") and len(parts) > 1:
        return parts[1]
    if head == "suggest":
        return "suggest_next_step"
    if head in ("card", "cards"):
        return "cards"
    return head or "unknown"


class StreamingAdapter(MessengerAdapter):
    """Puts graph outputs into an asyncio.Queue for SSE streaming.

    send_text streams text in small chunks with a delay to produce a typing effect.
    Event type `text_delta` carries {"delta": "<chunk>"} — clients accumulate deltas.

    send_text_with_buttons / send_text_with_keyboard carry inline-keyboard prompts
    (clarify cards, gender pick, pick_item carousel, ...) as a single `clarify` event
    — {"axis": "...", "prompt": "...", "options": [{"label", "callback"}, ...]}.
    """

    _CHUNK_SIZE = 3  # characters per text_delta event
    _CHUNK_DELAY = 0.030  # seconds between chunks (~100 chars/s typing speed)

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict] | object] = asyncio.Queue()
        self._texts: list[str] = []
        self._cards: list[BotCard] = []

    async def parse_inbound(self, payload: dict) -> ChannelMessage:
        raise NotImplementedError("StreamingAdapter is send-only")

    async def send_text(self, chat_id: int, text: str) -> None:
        self._texts.append(text)
        for i in range(0, len(text), self._CHUNK_SIZE):
            chunk = text[i : i + self._CHUNK_SIZE]
            await self._queue.put(("text_delta", {"delta": chunk}))
            if i + self._CHUNK_SIZE < len(text):
                await asyncio.sleep(self._CHUNK_DELAY)

    async def send_card(self, chat_id: int, card: BotCard) -> int | None:
        self._cards.append(card)
        await self._queue.put(
            ("product", {"image_url": str(card.image_url), "caption": card.caption, "product_id": card.product_id})
        )
        return 0

    async def send_text_with_buttons(self, chat_id: int, text: str, buttons: list[tuple[str, str]]) -> int | None:
        self._texts.append(text)  # keep get_reply() history parity with send_text
        axis = _infer_clarify_axis(buttons[0][1]) if buttons else "unknown"
        await self._queue.put(
            (
                "clarify",
                {
                    "axis": axis,
                    "prompt": text,
                    "options": [{"label": label, "callback": cb} for label, cb in buttons],
                },
            )
        )
        return None

    async def send_text_with_keyboard(
        self, chat_id: int, text: str, keyboard: list[list[tuple[str, str]]]
    ) -> int | None:
        # Mobile renders options as a vertical list — row grouping (a Telegram
        # inline-keyboard concern) doesn't matter here, so flatten.
        flat = [pair for row in keyboard for pair in row]
        return await self.send_text_with_buttons(chat_id, text, flat)

    def close(self) -> None:
        self._queue.put_nowait(_SENTINEL)

    async def iter_events(self) -> AsyncGenerator[tuple[str, dict]]:
        while True:
            item = await self._queue.get()
            if item is _SENTINEL:
                return
            yield item  # type: ignore[misc]

    def get_reply(self) -> BotReply:
        texts = self._texts[:]
        closing = texts[-1] if len(texts) > 1 else None
        main_text = texts[0] if texts else None
        return BotReply(
            text=main_text,
            cards=list(self._cards),
            closing_text=closing if closing != main_text else None,
        )


# ── DB helpers ────────────────────────────────────────────────────────────────


async def get_or_create_session(
    pool: AsyncConnectionPool,
    user_id: UUID,
    session_id: UUID | None,
) -> UUID:
    """Return existing session or create a new one."""
    if session_id is not None:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT session_id FROM ai.chat_sessions WHERE session_id = %s AND user_id = %s",
                (session_id, user_id),
            )
            row = await cur.fetchone()
        if row:
            return session_id
        # session_id given but doesn't belong to this user → create new
    new_id = uuid4()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO ai.chat_sessions (session_id, user_id) VALUES (%s, %s)",
            (new_id, user_id),
        )
    return new_id


async def append_message(
    pool: AsyncConnectionPool,
    session_id: UUID,
    role: str,
    content: str,
    product_refs: list[dict] | None = None,
) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.chat_messages (session_id, role, content, product_refs)
            VALUES (%s, %s, %s, %s)
            """,
            (session_id, role, content, Jsonb(product_refs) if product_refs is not None else None),
        )
        await cur.execute(
            "UPDATE ai.chat_sessions SET last_message_at = now() WHERE session_id = %s",
            (session_id,),
        )


async def set_session_title(
    pool: AsyncConnectionPool,
    session_id: UUID,
    title: str,
) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE ai.chat_sessions SET title = %s WHERE session_id = %s AND title IS NULL",
            (title[:80], session_id),
        )


# ── Core invoke ───────────────────────────────────────────────────────────────


def _bind_chat_trace(session_id: UUID, user_id: UUID, text: str, *, turn_id: str) -> list:
    """Bind the current Langfuse trace to this chat turn + return graph callbacks.

    Mirrors the telegram webhook trace setup (telegram.py) so native-app (chat)
    turns produce the same unified conversation traces: a root trace bound to
    session/user + a CallbackHandler bridging nested LLM generations. `user_id`
    is hashed (pii.hash_id); `session_id` is the chat-session UUID (internal id,
    used as the Langfuse session grouping key).
    """
    sid = str(session_id)
    uid = hash_id(str(user_id))
    update_current_trace(
        name="app.chat",
        session_id=sid,
        user_id=uid,
        input={"channel": "app", "text": text[:200]},
        metadata={"channel": "app", "graph": "fashion_bot", "turn_id": turn_id},
    )
    handler = build_callback_handler(
        session_id=sid,
        user_id=uid,
        metadata={"channel": "app", "graph": "fashion_bot", "turn_id": turn_id},
    )
    return [handler] if handler is not None else []


def _reset_app_turn(user_id: UUID, synthetic_chat_id: int, thread_id: UUID, turn_no: int) -> str:
    turn_id = f"{thread_id}:{turn_no}"
    reset_turn(
        turn_id=turn_id,
        user_key=user_key_for(None, synthetic_chat_id),
        chat_id=synthetic_chat_id,
        thread_id=thread_id,
        turn_no=turn_no,
    )
    return turn_id


async def _persist_search(pool: AsyncConnectionPool, user_id: UUID, session_id: UUID, title: str) -> str | None:
    """그래프 결과셋(sess.last_results)을 ai.searches 한 행으로 영속화하고 search_id 반환.

    결과가 없으면 None (빈 검색은 결과셋이 아니므로 미저장). fail-open.

    260701 — is_listed 기본값 TRUE 로 변경. 이전에는 사용자가 명시적으로
    "더보기" (`/v1/results/{search_id}`) 를 열어야 히스토리 피드에 노출됐는데,
    모바일 UX 상 채팅 카드로 이미 결과를 본 시점이 "검색이 발생한" 시점이라
    히스토리에 바로 남아야 함 (라이브 피드백 2026-07-01: 대화 세션 중 검색
    결과가 히스토리 탭에 안 뜬다). `/v1/results/{id}` 의 promotion UPDATE 는
    유지 — is_listed 컬럼 자체의 다른 활용 여지는 남겨둠.
    """
    try:
        from app.infrastructure.memory.session import get_store

        sess = get_store().get_or_create(_user_id_to_chat_id(user_id))
        candidates = list(getattr(sess, "last_results", None) or [])
    except Exception:
        logger.debug("[chat_service] search persist: session read failed", exc_info=True)
        return None

    product_ids: list[int] = []
    cover_image_urls: list[str] = []
    for c in candidates:
        pid = getattr(c, "id", None)
        if pid is not None:
            try:
                product_ids.append(int(pid))
            except (TypeError, ValueError):
                pass
        img = getattr(c, "image_url", None)
        if img and len(cover_image_urls) < 5:
            cover_image_urls.append(str(img))

    if not product_ids:
        return None

    new_id = uuid4()
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO ai.searches
                    (search_id, session_id, user_id, title, product_ids, cover_image_urls, total, is_listed)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                """,
                (new_id, session_id, user_id, title[:120], product_ids, cover_image_urls, len(product_ids)),
            )
    except Exception:
        logger.exception("[chat_service] search persist failed user=%s", user_id)
        return None
    return str(new_id)


@observe(name="app.chat", as_type="span")
async def invoke(
    user_id: UUID,
    text: str,
    pool: AsyncConnectionPool,
    session_id: UUID | None = None,
    *,
    gender: str | None = None,
    price_max: int | None = None,
) -> tuple[UUID, BotReply]:
    """Invoke the fashion bot graph for a consumer user.

    Returns (session_id, reply) where reply contains the AI response text + product cards.
    """
    resolved_session_id = await get_or_create_session(pool, user_id, session_id)
    await _sync_gender_to_taste_profile(pool, user_id, _user_id_to_chat_id(user_id))

    # Persist user message
    await append_message(pool, resolved_session_id, "user", text)
    # Set session title from first message
    await set_session_title(pool, resolved_session_id, text)

    synthetic_chat_id = _user_id_to_chat_id(user_id)
    urls = _URL_RE.findall(text or "")
    message = ChannelMessage(
        chat_id=synthetic_chat_id,
        text=text,
        urls=urls,
        received_at=datetime.now(UTC),
    )
    thread_id = uuid4()
    turn_no = 0
    input_state = InputState(
        message=message,
        chat_id=synthetic_chat_id,
        thread_id=thread_id,
        turn_no=turn_no,
        req_gender=gender,
        req_price_max=price_max,
    )

    capture = CaptureAdapter()
    token = set_adapter(capture)
    turn_id = _reset_app_turn(user_id, synthetic_chat_id, thread_id, turn_no)
    try:
        callbacks = _bind_chat_trace(resolved_session_id, user_id, text, turn_id=turn_id)
        await GRAPH.ainvoke(input_state, config={"callbacks": callbacks})
    except Exception:
        logger.exception("[chat_service] graph invocation failed user=%s", user_id)
        raise
    finally:
        reset_adapter(token)
        clear_turn()

    reply = capture.get_reply()

    # Persist assistant reply
    product_refs = None
    if reply.cards:
        product_refs = [
            {"image_url": str(c.image_url), "caption": c.caption, "product_id": c.product_id} for c in reply.cards
        ]
    assistant_content = reply.text or ""
    if reply.closing_text:
        assistant_content = f"{assistant_content}\n\n{reply.closing_text}".strip()
    await append_message(pool, resolved_session_id, "assistant", assistant_content, product_refs)
    await _persist_search(pool, user_id, resolved_session_id, text)

    return resolved_session_id, reply


async def invoke_streaming(
    user_id: UUID,
    text: str,
    pool: AsyncConnectionPool,
    session_id: UUID | None = None,
    *,
    gender: str | None = None,
    price_max: int | None = None,
    attached_image_url: str | None = None,
) -> AsyncGenerator[tuple[str, dict]]:
    """Invoke the fashion bot graph and yield (event_type, payload) tuples for SSE.

    Event sequence: session → text* → product* → done   (or error on failure)
    """
    synthetic_chat_id = _user_id_to_chat_id(user_id)
    resolved_session_id = await get_or_create_session(pool, user_id, session_id)
    cap_status = await get_app_cap_status(pool, user_id)

    yield "session", {"session_id": str(resolved_session_id), **cap_status.session_payload()}

    if cap_status.cap_reached:
        logger.info(
            "[chat_service] daily cap reached user=%s tier=%s used=%d cap=%d",
            user_id,
            cap_status.user_tier,
            cap_status.cap_used,
            cap_status.daily_cap,
        )
        try:
            from app.channels.lang import detect_lang
            from app.infrastructure.memory.taste_profile import user_key_for
            from app.observability.conversation_log import emit
            from app.observability.event_payloads import CapReachedPayload

            emit(
                event_type="cap_reached",
                user_key=user_key_for(None, synthetic_chat_id),
                chat_id=synthetic_chat_id,
                thread_id=uuid4(),
                turn_no=0,
                payload=CapReachedPayload(lang=detect_lang(text)),
            )
        except Exception:
            logger.debug("[chat_service] cap_reached emit skipped", exc_info=True)
        yield "cap_reached", cap_status.cap_event_payload()
        yield "done", {}
        return

    await _sync_gender_to_taste_profile(pool, user_id, synthetic_chat_id)
    await append_message(pool, resolved_session_id, "user", text)
    await set_session_title(pool, resolved_session_id, text)

    # attached_image_url (explicit upload) takes priority over a URL pasted in
    # free text — it's a deliberate attach action, not an incidental link.
    urls = ([attached_image_url] if attached_image_url else []) + _URL_RE.findall(text or "")
    message = ChannelMessage(
        chat_id=synthetic_chat_id,
        text=text,
        urls=urls,
        received_at=datetime.now(UTC),
    )
    thread_id = uuid4()
    turn_no = 0
    input_state = InputState(
        message=message,
        chat_id=synthetic_chat_id,
        thread_id=thread_id,
        turn_no=turn_no,
        req_gender=gender,
        req_price_max=price_max,
    )

    streaming = StreamingAdapter()
    graph_exc: BaseException | None = None

    @observe(name="app.chat", as_type="span")
    async def _run_graph() -> None:
        nonlocal graph_exc
        # set_adapter/reset_adapter must run in the same context (task's own copy).
        # asyncio.create_task copies the context at creation time; the Token from the
        # parent context cannot be used to reset a ContextVar inside the task.
        # @observe here creates the root Langfuse trace inside the task's own
        # context (the SSE generator runs in the outer context), so nested
        # node/LLM spans nest under a single conversation trace.
        token = set_adapter(streaming)
        turn_id = _reset_app_turn(user_id, synthetic_chat_id, thread_id, turn_no)
        try:
            callbacks = _bind_chat_trace(resolved_session_id, user_id, text, turn_id=turn_id)
            await GRAPH.ainvoke(input_state, config={"callbacks": callbacks})
        except Exception as exc:
            logger.exception("[chat_service] graph invocation failed user=%s", user_id)
            graph_exc = exc
        finally:
            streaming.close()
            reset_adapter(token)
            clear_turn()

    graph_task = asyncio.create_task(_run_graph())

    async for event_type, payload in streaming.iter_events():
        yield event_type, payload

    await graph_task

    if graph_exc is not None:
        yield "error", {"detail": "AI response failed"}
        return

    reply = streaming.get_reply()
    product_refs = None
    if reply.cards:
        product_refs = [
            {"image_url": str(c.image_url), "caption": c.caption, "product_id": c.product_id} for c in reply.cards
        ]
    assistant_content = reply.text or ""
    if reply.closing_text:
        assistant_content = f"{assistant_content}\n\n{reply.closing_text}".strip()
    await append_message(pool, resolved_session_id, "assistant", assistant_content, product_refs)

    search_id = await _persist_search(pool, user_id, resolved_session_id, text)
    if search_id is not None:
        yield "search", {"search_id": search_id}

    yield "done", {}


async def invoke_streaming_callback(
    user_id: UUID,
    session_id: UUID,
    callback_data: str,
    pool: AsyncConnectionPool,
    *,
    label: str | None = None,
) -> AsyncGenerator[tuple[str, dict]]:
    """Invoke the fashion bot graph for a button-tap (clarify/gender/pick_item callback)
    and yield (event_type, payload) tuples for SSE — the app-side counterpart of the
    Telegram webhook's callback_query handling.

    `session_id` ownership is verified by the caller (route) before this runs.
    Cap gating is skipped — matches the Telegram webhook, which lets callback taps
    through even over-cap since they're cheap UI actions, not new generations
    (app/api/webhooks/telegram.py `_invoke_graph`).

    thread_id/turn_no are freshly minted per callback (no card_sent DB correlation
    like Telegram's `_resolve_thread_id`) — graph routing (`_route_after_ingest_v2`)
    decides purely from the `callback_data` string, so this only affects conversation-
    log thread grouping, not behavior.
    """
    synthetic_chat_id = _user_id_to_chat_id(user_id)
    cap_status = await get_app_cap_status(pool, user_id)
    yield "session", {"session_id": str(session_id), **cap_status.session_payload()}

    if label:
        await append_message(pool, session_id, "user", label)

    trace_text = label or callback_data
    message = ChannelMessage(
        chat_id=synthetic_chat_id,
        callback_data=callback_data,
        received_at=datetime.now(UTC),
    )
    thread_id = uuid4()
    turn_no = 0
    input_state = InputState(
        message=message,
        chat_id=synthetic_chat_id,
        thread_id=thread_id,
        turn_no=turn_no,
    )

    streaming = StreamingAdapter()
    graph_exc: BaseException | None = None

    @observe(name="app.chat", as_type="span")
    async def _run_graph() -> None:
        nonlocal graph_exc
        token = set_adapter(streaming)
        turn_id = _reset_app_turn(user_id, synthetic_chat_id, thread_id, turn_no)
        try:
            callbacks = _bind_chat_trace(session_id, user_id, trace_text, turn_id=turn_id)
            await GRAPH.ainvoke(input_state, config={"callbacks": callbacks})
        except Exception as exc:
            logger.exception("[chat_service] callback graph invocation failed user=%s", user_id)
            graph_exc = exc
        finally:
            streaming.close()
            reset_adapter(token)
            clear_turn()

    graph_task = asyncio.create_task(_run_graph())

    async for event_type, payload in streaming.iter_events():
        yield event_type, payload

    await graph_task

    if graph_exc is not None:
        yield "error", {"detail": "AI response failed"}
        return

    reply = streaming.get_reply()
    product_refs = None
    if reply.cards:
        product_refs = [
            {"image_url": str(c.image_url), "caption": c.caption, "product_id": c.product_id} for c in reply.cards
        ]
    assistant_content = reply.text or ""
    if reply.closing_text:
        assistant_content = f"{assistant_content}\n\n{reply.closing_text}".strip()
    if assistant_content:
        await append_message(pool, session_id, "assistant", assistant_content, product_refs)

    search_id = await _persist_search(pool, user_id, session_id, trace_text)
    if search_id is not None:
        yield "search", {"search_id": search_id}

    yield "done", {}
