"""SPEC-CONVERSATION-LOG-001 / LOG-T08 — webhook intake thread_id propagation.

Verifies that non-callback Updates (text / photo):
- Seed a fresh `thread_id` (uuid4) and `turn_no=0`.
- Emit `user_text` / `user_photo` events to `ai.log_conversation_event`.
- Pass the resolved (`thread_id`, `turn_no`) through to `InputState`.

These tests exercise the real webhook endpoint via FastAPI test client and
verify the resulting row in the database (testcontainers Postgres).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def webhook_client(conv_log_backend_postgres) -> AsyncGenerator[AsyncClient]:
    """ASGI test client with `conv_log_backend` forced to postgres."""
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _drain_inflight() -> None:
    """Yield to the event loop so `asyncio.create_task` log writes complete."""
    from app.observability.conversation_log import _IN_FLIGHT

    for _ in range(50):
        if not _IN_FLIGHT:
            return
        await asyncio.sleep(0.01)


async def _fetch_log_rows(event_type: str) -> list[tuple]:
    from app.providers.db_pool import get_pool

    async with get_pool().connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT user_key, chat_id, thread_id, turn_no, event_type, payload "
            "FROM ai.log_conversation_event WHERE event_type = %s ORDER BY id",
            (event_type,),
        )
        return await cur.fetchall()


@pytest.mark.asyncio
async def test_text_intake_emits_user_text_with_fresh_thread_id(
    webhook_client: AsyncClient,
):
    """Text Update → one `user_text` row, fresh UUID thread_id, turn_no=0."""
    secret = "phase2-secret"
    fake_msg_payload = {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "chat": {"id": 42},
            "from": {"id": 7, "username": "alice"},
            "text": "안녕하세요",
        },
    }

    # Stub the graph so we don't run the full LangGraph in this test.
    with (
        patch("app.api.webhooks.telegram.settings.TELEGRAM_WEBHOOK_SECRET", secret),
        patch("app.api.webhooks.telegram._invoke_graph", new=AsyncMock()),
    ):
        resp = await webhook_client.post(
            "/webhooks/telegram",
            json=fake_msg_payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )

    assert resp.status_code == 200
    await _drain_inflight()

    rows = await _fetch_log_rows("user_text")
    assert len(rows) == 1, f"expected 1 user_text row, got {len(rows)}"
    user_key, chat_id, thread_id, turn_no, event_type, payload = rows[0]
    assert user_key == "u:7"
    assert chat_id == 42
    assert thread_id is not None  # fresh uuid4
    assert turn_no == 0
    assert event_type == "user_text"
    assert payload["text"] == "안녕하세요"
    assert payload["lang_detected"] == "ko"


@pytest.mark.asyncio
async def test_photo_intake_emits_user_photo_with_fresh_thread_id(
    webhook_client: AsyncClient,
):
    """Photo Update → one `user_photo` row, fresh UUID thread_id, turn_no=0."""
    secret = "phase2-secret"
    fake_msg_payload = {
        "update_id": 101,
        "message": {
            "message_id": 2,
            "chat": {"id": 99},
            "from": {"id": 88, "username": "bob"},
            "photo": [
                {"file_id": "small-id", "file_size": 100},
                {"file_id": "big-id", "file_size": 500},
            ],
            "caption": "cute jacket",
        },
    }

    with (
        patch("app.api.webhooks.telegram.settings.TELEGRAM_WEBHOOK_SECRET", secret),
        patch("app.api.webhooks.telegram._invoke_graph", new=AsyncMock()),
    ):
        resp = await webhook_client.post(
            "/webhooks/telegram",
            json=fake_msg_payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )

    assert resp.status_code == 200
    await _drain_inflight()

    rows = await _fetch_log_rows("user_photo")
    assert len(rows) == 1
    user_key, chat_id, thread_id, turn_no, event_type, payload = rows[0]
    assert user_key == "u:88"
    assert chat_id == 99
    assert thread_id is not None
    assert turn_no == 0
    assert event_type == "user_photo"
    # Largest photo's file_id is chosen by the adapter.
    assert payload["attachment_id"] == "big-id"
    assert payload["caption"] == "cute jacket"


@pytest.mark.asyncio
async def test_input_state_receives_resolved_thread_id_and_turn_no(
    webhook_client: AsyncClient,
):
    """The graph receives the same `thread_id` / `turn_no` used in the emit."""
    secret = "phase2-secret"
    fake_msg_payload = {
        "update_id": 102,
        "message": {
            "message_id": 3,
            "chat": {"id": 11},
            "from": {"id": 22},
            "text": "hello",
        },
    }

    captured: dict = {}

    async def _capture(adapter, message, thread_id, turn_no):
        captured["thread_id"] = thread_id
        captured["turn_no"] = turn_no

    with (
        patch("app.api.webhooks.telegram.settings.TELEGRAM_WEBHOOK_SECRET", secret),
        patch("app.api.webhooks.telegram._invoke_graph", side_effect=_capture),
    ):
        resp = await webhook_client.post(
            "/webhooks/telegram",
            json=fake_msg_payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )
    assert resp.status_code == 200
    # BackgroundTasks run after the response returns; drain.
    for _ in range(50):
        if "thread_id" in captured:
            break
        await asyncio.sleep(0.01)
    await _drain_inflight()

    rows = await _fetch_log_rows("user_text")
    assert len(rows) == 1
    _, _, row_thread_id, row_turn_no, _, _ = rows[0]
    assert captured["thread_id"] == row_thread_id, "graph must receive the same thread_id that was emitted"
    assert captured["turn_no"] == row_turn_no == 0
