"""SPEC-AGENT-V2-REACT review P1-1 — webhook callback_data log is bounded.

The intake INFO log caps every user-controlled field (user_id hashed,
text [:80]); `cb` must be bounded the same way ([:64]) instead of raw.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.channels.schemas import ChannelMessage


@pytest.mark.asyncio
async def test_webhook_callback_data_capped_in_log(client: AsyncClient, caplog):
    long_cb = "clarify:" + ("A" * 500)  # far beyond the 64-char cap
    fake_msg = ChannelMessage(
        chat_id=42,
        from_user_id=99,
        text=None,
        urls=[],
        callback_data=long_cb,
        received_at=datetime.now(tz=UTC),
    )
    secret = "test-secret-xyz"
    with (
        patch("app.api.webhooks.telegram.settings.TELEGRAM_WEBHOOK_SECRET", secret),
        patch("app.api.webhooks.telegram.get_adapter") as get_adapter_mock,
        patch(
            "app.api.webhooks.telegram._emit_intake_and_resolve_thread",
            new=AsyncMock(return_value=("thread-1", 1)),
        ),
        patch("app.api.webhooks.telegram._run_graph_safe", new=AsyncMock()),
    ):
        adapter = AsyncMock()
        adapter.parse_inbound = AsyncMock(return_value=fake_msg)
        get_adapter_mock.return_value = adapter

        with caplog.at_level(logging.INFO, logger="app.api.webhooks.telegram"):
            resp = await client.post(
                "/webhooks/telegram",
                json={"update_id": 1},
                headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            )

    assert resp.status_code == 200
    intake = [r for r in caplog.records if "inbound update_id=" in r.getMessage()]
    assert intake, "intake INFO log not emitted"
    rendered = intake[0].getMessage()
    # Raw 500-char payload must NOT appear; cap is 64 chars.
    assert long_cb not in rendered
    assert long_cb[:64] in rendered
    assert "A" * 65 not in rendered
