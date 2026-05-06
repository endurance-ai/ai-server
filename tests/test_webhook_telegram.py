"""SPEC-AGENT-001 / REQ-MIGR-004 + REQ-COMPAT-009 — webhook caller swap.

Verifies that the Telegram webhook still preserves the HTTP 200 / 401 contract
from SPEC-MSG-001 REQ-MSG-001/002 after the scenario→graph migration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.channels.schemas import ChannelMessage


@pytest.fixture
async def authed_client(client: AsyncClient):
    """Return a client + a valid secret to send with the X-Telegram-Bot-Api-Secret-Token header."""
    yield client


async def test_webhook_rejects_missing_secret(client: AsyncClient):
    """REQ-COMPAT-009 — secret-token verification preserved."""
    with patch("app.api.webhooks.telegram.settings.TELEGRAM_WEBHOOK_SECRET", "expected-secret"):
        resp = await client.post("/webhooks/telegram", json={"update_id": 1})
    assert resp.status_code == 401


async def test_webhook_returns_200_on_valid_payload(client: AsyncClient):
    """REQ-MIGR-004 — graph invocation does not break the 200 contract."""
    fake_msg = ChannelMessage(
        chat_id=42,
        text="hi",
        urls=[],
        received_at=datetime.now(tz=UTC),
    )
    secret = "test-secret-xyz"
    with (
        patch("app.api.webhooks.telegram.settings.TELEGRAM_WEBHOOK_SECRET", secret),
        patch("app.api.webhooks.telegram.get_adapter") as get_adapter_mock,
        patch("app.api.webhooks.telegram._invoke_graph", new=AsyncMock()),
    ):
        adapter = AsyncMock()
        adapter.parse_inbound = AsyncMock(return_value=fake_msg)
        get_adapter_mock.return_value = adapter

        resp = await client.post(
            "/webhooks/telegram",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
