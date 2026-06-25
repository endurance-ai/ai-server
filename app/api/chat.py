"""Consumer chat API (SPEC-AUTH-SOCIAL-001 Phase 1-C).

POST /chat/sessions                        — start new session (SSE stream)
POST /chat/sessions/{session_id}/messages  — continue existing session (SSE stream)
GET  /chat/sessions                        — list user's sessions
GET  /chat/sessions/{session_id}/messages  — paginated message history

SSE event sequence for POST endpoints:
  event: session  data: {"session_id": "<uuid>"}
  event: text     data: {"text": "<reply text>"}      # 0-N times
  event: product  data: {"image_url": "...", "caption": "..."}  # 0-N times
  event: done     data: {}
  event: error    data: {"detail": "..."}             # on failure
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool
from app.services import chat_service

router = APIRouter(prefix="/chat", tags=["chat"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _to_sse(gen: AsyncGenerator[tuple[str, dict]]) -> AsyncGenerator[str]:
    async for event_type, payload in gen:
        yield f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── Schemas ───────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str


class ProductRef(BaseModel):
    image_url: str | None = None
    caption: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply_text: str | None
    products: list[ProductRef]


class SessionSummary(BaseModel):
    session_id: str
    title: str | None
    last_message_at: str | None


class MessageItem(BaseModel):
    message_id: str
    role: str
    content: str
    product_refs: list[ProductRef] | None
    created_at: str


class MessageListResponse(BaseModel):
    messages: list[MessageItem]
    next_cursor: str | None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post("/sessions", status_code=status.HTTP_200_OK)
async def create_session(
    body: ChatRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> StreamingResponse:
    """Start a new chat session. Returns an SSE stream."""
    if not body.message.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="message cannot be empty")

    gen = chat_service.invoke_streaming(user_id, body.message, pool)
    return StreamingResponse(_to_sse(gen), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/sessions/{session_id}/messages", status_code=status.HTTP_200_OK)
async def continue_session(
    session_id: UUID,
    body: ChatRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> StreamingResponse:
    """Continue an existing chat session. Returns an SSE stream."""
    if not body.message.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="message cannot be empty")

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM ai.chat_sessions WHERE session_id = %s AND user_id = %s",
            (session_id, user_id),
        )
        if not await cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    gen = chat_service.invoke_streaming(user_id, body.message, pool, session_id=session_id)
    return StreamingResponse(_to_sse(gen), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/sessions", response_model=list[SessionSummary], status_code=status.HTTP_200_OK)
async def list_sessions(
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> list[SessionSummary]:
    """Return all chat sessions for the current user, newest first."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT session_id, title, last_message_at
            FROM ai.chat_sessions
            WHERE user_id = %s
            ORDER BY COALESCE(last_message_at, created_at) DESC
            LIMIT 50
            """,
            (user_id,),
        )
        rows = await cur.fetchall()

    return [
        SessionSummary(
            session_id=str(r[0]),
            title=r[1],
            last_message_at=r[2].isoformat() if r[2] else None,
        )
        for r in rows
    ]


@router.get("/sessions/{session_id}/messages", response_model=MessageListResponse, status_code=status.HTTP_200_OK)
async def list_messages(
    session_id: UUID,
    cursor: str | None = Query(default=None, description="Pagination cursor (message_id)"),
    limit: int = Query(default=20, ge=1, le=100),
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> MessageListResponse:
    """Return paginated messages for a session (oldest-first within page)."""
    # Verify ownership
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM ai.chat_sessions WHERE session_id = %s AND user_id = %s",
            (session_id, user_id),
        )
        if not await cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    async with pool.connection() as conn, conn.cursor() as cur:
        if cursor:
            await cur.execute(
                """
                SELECT message_id, role, content, product_refs, created_at
                FROM ai.chat_messages
                WHERE session_id = %s
                  AND created_at > (SELECT created_at FROM ai.chat_messages WHERE message_id = %s)
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (session_id, UUID(cursor), limit + 1),
            )
        else:
            await cur.execute(
                """
                SELECT message_id, role, content, product_refs, created_at
                FROM ai.chat_messages
                WHERE session_id = %s
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (session_id, limit + 1),
            )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = str(page[-1][0]) if has_more and page else None

    messages = [
        MessageItem(
            message_id=str(r[0]),
            role=r[1],
            content=r[2],
            product_refs=[ProductRef(**p) for p in r[3]] if r[3] else None,
            created_at=r[4].isoformat(),
        )
        for r in page
    ]
    return MessageListResponse(messages=messages, next_cursor=next_cursor)
