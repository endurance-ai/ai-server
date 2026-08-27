"""Consumer chat API (SPEC-AUTH-SOCIAL-001 Phase 1-C).

POST /v1/chat/sessions                        — start new session (SSE stream)
POST /v1/chat/sessions/{session_id}/messages  — continue existing session (SSE stream)
GET  /v1/chat/sessions                        — list user's sessions
GET  /v1/chat/sessions/{session_id}/messages  — paginated message history

SSE event sequence for POST endpoints:
  event: session  data: {"session_id": "<uuid>", "user_tier": "free|basic|pro", ...cap metadata}
  event: text     data: {"text": "<reply text>"}      # 0-N times
  event: product  data: {"image_url": "...", "caption": "...", "product_id": 123}  # 0-N times
  event: clarify  data: {"axis": "pick_item|gender|...", "prompt": "...",
                         "options": [{"label": "...", "callback": "item:0"}, ...]}  # 0-N times
  event: cap_reached data: {"code": "daily_token_cap_reached", "user_tier": "...", ...}  # cap hit
  event: done     data: {"status": "success|conversational_fallback|zero_results",
                         "ai_response_type": "product_grid|text_retry_prompt|mixed"}  # type optional
  event: error    data: {"detail": "..."}             # on failure

POST /v1/chat/sessions/{session_id}/callback — send a button tap (`clarify` event's
`options[i].callback`). Returns the same SSE stream.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, field_validator

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool
from app.services import chat_service

router = APIRouter(prefix="/v1/chat", tags=["chat"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _to_sse(gen: AsyncGenerator[tuple[str, dict]]) -> AsyncGenerator[str]:
    async for event_type, payload in gen:
        yield f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── Schemas ───────────────────────────────────────────────────────────────────


# Mobile filter UI sends free-form gender labels (codes or KO labels). Normalize
# to the taste-token vocabulary the search pipeline understands
# (men/women/unisex). Unknown values normalize to None → "no gender filter".
_GENDER_NORMALIZE = {
    "men": "men",
    "male": "men",
    "man": "men",
    "m": "men",
    "남성": "men",
    "남자": "men",
    "women": "women",
    "female": "women",
    "woman": "women",
    "w": "women",
    "여성": "women",
    "여자": "women",
    "unisex": "unisex",
    "other": "unisex",
    "either": "unisex",
    "all": "unisex",
    "공용": "unisex",
    "상관없음": "unisex",
}


class ChatRequest(BaseModel):
    message: str
    # Per-request search filters from the mobile filter UI (FilterProvider).
    # gender: 공용/여성/남성 → unisex/women/men. Per-request only — never persists
    # to the user's taste profile (SPEC-GENDER-PIN-001).
    # price_max: upper price bound in KRW integer 원. <=0 / None → no ceiling.
    gender: str | None = None
    price_max: int | None = None
    # Image uploaded via POST /v1/uploads (presigned S3 PUT → CloudFront/CDN URL).
    # Passed through to ChannelMessage.urls — the existing SSRF guard there drops
    # it silently if malformed, same fail-open contract as the Pinterest-link path.
    attached_image_url: str | None = None
    # 앱 이미지 인풋 스테이징에서 사용자가 이미 항목을 골랐을 때 true — 이미지가
    # 첨부돼도 pick_item(1,2,3,4) 을 건너뛰고 바로 검색(이미지 임베딩 블렌드)으로.
    skip_item_pick: bool = False

    @field_validator("gender", mode="before")
    @classmethod
    def _normalize_gender(cls, v: object) -> str | None:
        if v is None:
            return None
        return _GENDER_NORMALIZE.get(str(v).strip().lower())

    @field_validator("price_max", mode="before")
    @classmethod
    def _clamp_price_max(cls, v: object) -> int | None:
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None


class ChatCallbackRequest(BaseModel):
    """Body for POST /sessions/{session_id}/callback — a button tap from a `clarify`
    SSE event (`options[i].callback`). `label` is the tapped button's display text,
    persisted as the user's chat-history turn when present (mirrors what the user
    "said" by tapping)."""

    callback_data: str
    label: str | None = None


class ProductRef(BaseModel):
    image_url: str | None = None
    caption: str | None = None
    # products.id — lets the client deep-link a chat card to its PDP
    # (GET /v1/products/{id}) and fire Record View. None for legacy rows /
    # id-less candidates.
    product_id: int | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply_text: str | None
    products: list[ProductRef]


class SessionSummary(BaseModel):
    session_id: str
    title: str | None
    last_message_at: str | None


class SessionRenameRequest(BaseModel):
    title: str


class MessageItem(BaseModel):
    message_id: str
    role: str
    content: str
    product_refs: list[ProductRef] | None
    # ai.searches.search_id for the result set this (assistant) turn produced — lets
    # the client rebuild the "더보기" button on history restore. None when the turn had
    # no result set (user messages, text-only replies).
    search_id: str | None = None
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

    gen = chat_service.invoke_streaming(
        user_id,
        body.message,
        pool,
        gender=body.gender,
        price_max=body.price_max,
        attached_image_url=body.attached_image_url,
        skip_item_pick=body.skip_item_pick,
    )
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

    gen = chat_service.invoke_streaming(
        user_id,
        body.message,
        pool,
        session_id=session_id,
        gender=body.gender,
        price_max=body.price_max,
        attached_image_url=body.attached_image_url,
        skip_item_pick=body.skip_item_pick,
    )
    return StreamingResponse(_to_sse(gen), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/sessions/{session_id}/callback", status_code=status.HTTP_200_OK)
async def send_callback(
    session_id: UUID,
    body: ChatCallbackRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> StreamingResponse:
    """Send a button tap (a `clarify` event's `options[i].callback`). Returns an SSE stream."""
    if not body.callback_data.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="callback_data cannot be empty")

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM ai.chat_sessions WHERE session_id = %s AND user_id = %s",
            (session_id, user_id),
        )
        if not await cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    gen = chat_service.invoke_streaming_callback(user_id, session_id, body.callback_data, pool, label=body.label)
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


@router.patch("/sessions/{session_id}", response_model=SessionSummary, status_code=status.HTTP_200_OK)
async def rename_session(
    session_id: UUID,
    body: SessionRenameRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> SessionSummary:
    """세션 제목 변경."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE ai.chat_sessions
            SET title = %s, last_message_at = COALESCE(last_message_at, now())
            WHERE session_id = %s AND user_id = %s
            RETURNING session_id, title, last_message_at
            """,
            (body.title, session_id, user_id),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return SessionSummary(
        session_id=str(row[0]),
        title=row[1],
        last_message_at=row[2].isoformat() if row[2] else None,
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> None:
    """세션 및 하위 메시지 삭제 (CASCADE)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM ai.chat_sessions WHERE session_id = %s AND user_id = %s RETURNING session_id",
            (session_id, user_id),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")


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
                SELECT message_id, role, content, product_refs, search_id, created_at
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
                SELECT message_id, role, content, product_refs, search_id, created_at
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
            search_id=str(r[4]) if r[4] else None,
            created_at=r[5].isoformat(),
        )
        for r in page
    ]
    return MessageListResponse(messages=messages, next_cursor=next_cursor)
