"""Consumer chat service — bridges UUID-based user identity to the LangGraph fashion bot.

Strategy: CaptureAdapter
  The existing graph sends responses via MessengerAdapter.send_text / send_card.
  CaptureAdapter implements the interface but collects responses in-process
  instead of sending to Telegram. After graph invocation, collected responses
  are returned to the REST API caller and persisted to ai.chat_messages.

User identity bridge:
  The graph uses `chat_id: int` for session/taste-profile lookups.
  Consumer users have `user_id: UUID`. We derive a stable int from the UUID bytes
  so the same user always resolves to the same session key in the existing stores.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.channels.adapter import MessengerAdapter
from app.channels.schemas import BotCard, BotReply, ChannelMessage
from app.graphs.fashion_bot import GRAPH
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.state import InputState

logger = logging.getLogger(__name__)


def _user_id_to_chat_id(user_id: UUID) -> int:
    """Derive a stable positive int from a UUID for graph session key compatibility."""
    return abs(int.from_bytes(user_id.bytes[:8], "big")) % (2**62)


# user_profiles uses ('male','female','other'); taste_profile uses ('men','women','unisex')
_GENDER_MAP = {"male": "men", "female": "women", "other": "unisex"}


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


async def invoke(
    user_id: UUID,
    text: str,
    pool: AsyncConnectionPool,
    session_id: UUID | None = None,
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
    message = ChannelMessage(
        chat_id=synthetic_chat_id,
        text=text,
        received_at=datetime.now(UTC),
    )
    input_state = InputState(
        message=message,
        chat_id=synthetic_chat_id,
        thread_id=uuid4(),
        turn_no=0,
    )

    capture = CaptureAdapter()
    token = set_adapter(capture)
    try:
        await GRAPH.ainvoke(input_state)
    except Exception:
        logger.exception("[chat_service] graph invocation failed user=%s", user_id)
        raise
    finally:
        reset_adapter(token)

    reply = capture.get_reply()

    # Persist assistant reply
    product_refs = None
    if reply.cards:
        product_refs = [
            {"image_url": str(c.image_url), "caption": c.caption}
            for c in reply.cards
        ]
    assistant_content = reply.text or ""
    if reply.closing_text:
        assistant_content = f"{assistant_content}\n\n{reply.closing_text}".strip()
    await append_message(pool, resolved_session_id, "assistant", assistant_content, product_refs)

    return resolved_session_id, reply
