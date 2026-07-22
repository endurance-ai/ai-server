"""Unit tests for app/channels/discord_notify."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.channels import discord_notify


@pytest.fixture(autouse=True)
def _reset_client_state():
    discord_notify._client = None
    yield
    discord_notify._client = None


def _fake_client(status: int = 204) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = ""
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_empty_webhook_is_noop(monkeypatch):
    """Empty DISCORD_SIGNUP_WEBHOOK_URL must not touch httpx at all."""
    monkeypatch.setattr(discord_notify.settings, "DISCORD_SIGNUP_WEBHOOK_URL", "")
    client = _fake_client()
    monkeypatch.setattr(discord_notify, "_get_client", lambda: client)

    await discord_notify.notify_signup(provider="google", total_users=7)

    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_posts_anonymous_payload(monkeypatch):
    monkeypatch.setattr(discord_notify.settings, "DISCORD_SIGNUP_WEBHOOK_URL", "https://discord.test/webhook/xyz")
    client = _fake_client()
    monkeypatch.setattr(discord_notify, "_get_client", lambda: client)

    await discord_notify.notify_signup(provider="google", total_users=42)

    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args[0] == "https://discord.test/webhook/xyz"
    content = kwargs["json"]["content"]
    assert "42" in content
    assert "google" in content
    # No PII / user_id ever appears in the payload.
    assert "user_id" not in content
    assert "@" not in content


@pytest.mark.asyncio
async def test_http_error_is_swallowed(monkeypatch):
    """A Discord outage must never raise into the signup flow (fail-open)."""
    monkeypatch.setattr(discord_notify.settings, "DISCORD_SIGNUP_WEBHOOK_URL", "https://discord.test/webhook/xyz")
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
    monkeypatch.setattr(discord_notify, "_get_client", lambda: client)

    # Must not raise.
    await discord_notify.notify_signup(provider="apple", total_users=1)
