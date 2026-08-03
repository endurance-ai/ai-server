"""Direct APNs HTTP/2 provider with environment-specific token credentials."""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from jose import jwt
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

logger = logging.getLogger(__name__)

PushEnvironment = Literal["development", "production"]

_HOSTS: dict[PushEnvironment, str] = {
    "development": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}
_TOKEN_TTL_S = 55 * 60
_DEAD_TOKEN_REASONS = frozenset({"Unregistered", "ExpiredToken"})
_RETRYABLE_REASONS = frozenset({"TooManyRequests", "InternalServerError", "ServiceUnavailable", "Shutdown"})
_CONFIGURATION_REASONS = frozenset(
    {
        "BadEnvironmentKeyIdInToken",
        "DeviceTokenNotForTopic",
        "Forbidden",
        "InvalidProviderToken",
        "MissingProviderToken",
        "UnrelatedKeyIdInToken",
    }
)


@dataclass(frozen=True, slots=True)
class ApnsCredential:
    key: str
    key_id: str
    team_id: str
    topic: str


@dataclass(frozen=True, slots=True)
class PushEndpoint:
    device_id: UUID
    user_id: UUID
    push_token: str
    environment: PushEnvironment
    topic: str
    registered_at: datetime


@dataclass(frozen=True, slots=True)
class ApnsResult:
    device_token: str
    status: int
    reason: str | None = None
    apns_id: str | None = None
    invalidated_at: datetime | None = None
    retry_after_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def token_is_dead(self) -> bool:
        return self.reason in _DEAD_TOKEN_REASONS

    @property
    def retryable(self) -> bool:
        return self.status in (0, 429, 500, 503) or self.reason in _RETRYABLE_REASONS

    @property
    def configuration_error(self) -> bool:
        return self.reason in _CONFIGURATION_REASONS or self.status == 403


def _decode_key(encoded: str) -> str:
    if not encoded.strip():
        return ""
    try:
        return base64.b64decode(encoded, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        logger.error("🔔 [apns] invalid base64 auth key: %s", type(exc).__name__)
        return ""


def _read_key(path_value: str) -> str:
    if not path_value.strip():
        return ""
    path = Path(path_value)
    if path.is_file():
        return path.read_text()
    logger.warning("🔔 [apns] auth key path not found: %s", path)
    return ""


def credential(environment: PushEnvironment) -> ApnsCredential | None:
    if environment == "development":
        key = _decode_key(settings.APNS_SANDBOX_AUTH_KEY_B64) or _read_key(settings.APNS_SANDBOX_AUTH_KEY_PATH)
        key_id = settings.APNS_SANDBOX_KEY_ID
    else:
        key = (
            _decode_key(settings.APNS_PRODUCTION_AUTH_KEY_B64)
            or _read_key(settings.APNS_PRODUCTION_AUTH_KEY_PATH)
            or settings.APNS_AUTH_KEY
            or _read_key(settings.APNS_AUTH_KEY_PATH)
        )
        key_id = settings.APNS_PRODUCTION_KEY_ID or settings.APNS_KEY_ID

    topic = settings.APNS_TOPIC
    if not (key.strip() and key_id.strip() and settings.APNS_TEAM_ID.strip() and topic.strip()):
        return None
    return ApnsCredential(key=key, key_id=key_id, team_id=settings.APNS_TEAM_ID, topic=topic)


def apns_configured(environment: PushEnvironment = "production") -> bool:
    return credential(environment) is not None


class ApnsClient:
    """Long-lived HTTP/2 client for exactly one APNs environment."""

    def __init__(self, *, environment: PushEnvironment = "production") -> None:
        self.environment = environment
        self._credential = credential(environment)
        self._client = httpx.AsyncClient(
            base_url=_HOSTS[environment],
            http2=True,
            timeout=settings.APNS_TIMEOUT_S,
            limits=httpx.Limits(max_connections=max(1, settings.NOTIFY_APNS_CONCURRENCY)),
        )
        self._token = ""
        self._token_at = 0.0

    async def __aenter__(self) -> ApnsClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _provider_token(self) -> str:
        if self._credential is None:
            raise RuntimeError(f"APNs {self.environment} credentials are not configured")
        now = time.time()
        if self._token and now - self._token_at < _TOKEN_TTL_S:
            return self._token
        self._token = jwt.encode(
            {"iss": self._credential.team_id, "iat": int(now)},
            self._credential.key,
            algorithm="ES256",
            headers={"kid": self._credential.key_id},
        )
        self._token_at = now
        return self._token

    async def send(
        self,
        device_token: str,
        *,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        collapse_id: str | None = None,
        apns_id: str | None = None,
        priority: Literal[5, 10] = 10,
        expiration: datetime | None = None,
    ) -> ApnsResult:
        if self._credential is None:
            return ApnsResult(device_token=device_token, status=0, reason="MissingProviderToken", apns_id=apns_id)

        payload: dict[str, Any] = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
        if data:
            payload.update(data)

        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": self._credential.topic,
            "apns-push-type": "alert",
            "apns-priority": str(priority),
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        if apns_id:
            headers["apns-id"] = apns_id
        if expiration:
            headers["apns-expiration"] = str(int(expiration.timestamp()))

        try:
            response = await self._client.post(f"/3/device/{device_token}", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning(
                "🔔 [apns] transport error endpoint=%s error=%s",
                _fingerprint(device_token),
                type(exc).__name__,
            )
            return ApnsResult(device_token=device_token, status=0, reason="TransportError", apns_id=apns_id)

        response_apns_id = response.headers.get("apns-id") or apns_id
        if response.status_code == 200:
            return ApnsResult(device_token=device_token, status=200, apns_id=response_apns_id)

        reason: str | None = None
        invalidated_at: datetime | None = None
        try:
            response_body = response.json()
            reason = response_body.get("reason")
            timestamp_ms = response_body.get("timestamp")
            if timestamp_ms is not None:
                invalidated_at = datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=UTC)
        except (TypeError, ValueError):
            reason = response.text[:120] or None

        retry_after_s: float | None = None
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                retry_after_s = max(0.0, float(retry_after))
            except ValueError:
                retry_after_s = None

        logger.warning(
            "🔔 [apns] rejected endpoint=%s environment=%s status=%d reason=%s apns_id=%s",
            _fingerprint(device_token),
            self.environment,
            response.status_code,
            reason,
            response_apns_id,
        )
        return ApnsResult(
            device_token=device_token,
            status=response.status_code,
            reason=reason,
            apns_id=response_apns_id,
            invalidated_at=invalidated_at,
            retry_after_s=retry_after_s,
        )


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


async def fetch_endpoints(pool: AsyncConnectionPool, user_ids: list[UUID]) -> dict[UUID, list[PushEndpoint]]:
    if not user_ids:
        return {}
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT device_id, user_id, push_token, environment, topic, registered_at
            FROM ai.devices
            WHERE user_id = ANY(%s)
              AND provider = 'apns'
              AND platform = 'ios'
              AND status = 'active'
            """,
            (user_ids,),
        )
        rows = await cur.fetchall()
    result: dict[UUID, list[PushEndpoint]] = {}
    for row in rows:
        endpoint = PushEndpoint(
            device_id=row[0],
            user_id=row[1],
            push_token=row[2],
            environment=row[3],
            topic=row[4],
            registered_at=row[5],
        )
        result.setdefault(endpoint.user_id, []).append(endpoint)
    return result
