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


# 서명된 provider token 을 **자격증명 단위로** 모듈에 캐시한다: 캐시키 → (token, issued_at).
#
# Apple 은 20분보다 잦은 provider token 갱신을 `TooManyProviderTokenUpdates` (429) 로
# 거절한다. 캐시가 ApnsClient 인스턴스에만 있으면 인스턴스를 다시 만들 때마다 새 토큰이
# 서명되므로, 워커가 사이클마다 클라이언트를 세우던 시절엔 30초에 한 번씩 재서명됐다.
# 캐시를 인스턴스 밖에 두면 클라이언트 수명과 무관하게 TTL 이 실제로 지켜진다.
#
# 캐시키에 key_id 뿐 아니라 **키 본문 지문**을 넣는다. key_id 를 유지한 채 .p8 을 교체하면
# key_id 만으로는 낡은 토큰을 계속 내주게 된다. 지문을 섞으면 자격증명이 바뀌는 순간
# 자연히 새 항목이 된다.
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _token_cache_key(credential: ApnsCredential) -> str:
    return f"{credential.key_id}:{_fingerprint(credential.key)}"


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
        cache_key = _token_cache_key(self._credential)
        cached = _TOKEN_CACHE.get(cache_key)
        if cached and now - cached[1] < _TOKEN_TTL_S:
            return cached[0]
        token = jwt.encode(
            {"iss": self._credential.team_id, "iat": int(now)},
            self._credential.key,
            algorithm="ES256",
            headers={"kid": self._credential.key_id},
        )
        _TOKEN_CACHE[cache_key] = (token, now)
        return token

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


# ── 프로세스 수명 클라이언트 ─────────────────────────────────────────────────

_CLIENTS: dict[PushEnvironment, ApnsClient] = {}


def get_client(environment: PushEnvironment) -> ApnsClient:
    """환경당 하나의 클라이언트를 프로세스 수명으로 재사용한다.

    APNs 는 long-lived HTTP/2 연결을 전제한 프로토콜이다. 발송 사이클마다 클라이언트를
    세우고 닫으면 30초에 한 번씩 TLS 핸드셰이크를 새로 하고 커넥션 풀을 버리게 된다.
    provider token 은 `_TOKEN_CACHE` 가 따로 지키므로 재서명 문제와는 별개지만, 연결
    재사용은 이 레지스트리만이 해결한다.

    `ApnsClient` 를 직접 만드는 경로는 그대로 남는다 — 테스트가 그 seam 을 쓴다.
    """
    client = _CLIENTS.get(environment)
    if client is None:
        client = ApnsClient(environment=environment)
        _CLIENTS[environment] = client
    return client


async def close_clients() -> None:
    """프로세스 종료 시 열린 클라이언트를 모두 닫는다."""
    clients = list(_CLIENTS.values())
    _CLIENTS.clear()
    for client in clients:
        await client.aclose()


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
