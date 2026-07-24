"""Discord signup notification — async httpx, fire-and-forget.

A new-user signup fires a single POST to a Discord channel webhook
(`settings.DISCORD_SIGNUP_WEBHOOK_URL`). The message is anonymous — no PII,
no user_id — just the signup ordinal.

Fail-open: an empty webhook URL is a silent no-op, and any network error
is swallowed (logged only) so a Discord outage can never break signup.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0))
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def notify_signup(*, provider: str, total_users: int) -> None:
    """POST an anonymous signup notification to Discord. Never raises.

    `total_users` is the running count AFTER the insert, so it doubles as
    "this user is the Nth signup".
    """
    webhook_url = settings.DISCORD_SIGNUP_WEBHOOK_URL
    if not webhook_url:
        return  # feature off — silent no-op

    content = f"🎉 kikoai에 **{total_users}**번째 유저 가입! ({provider})"
    try:
        resp = await _get_client().post(webhook_url, json={"content": content})
        if resp.status_code >= 400:
            logger.warning("discord signup notify ❌ status=%d body=%s", resp.status_code, resp.text[:200])
    except httpx.HTTPError as e:
        logger.warning("discord signup notify ❌ http error: %r", e)
    except Exception as e:  # noqa: BLE001 — fail-open, must never break signup
        logger.warning("discord signup notify ❌ unexpected: %r", e)
