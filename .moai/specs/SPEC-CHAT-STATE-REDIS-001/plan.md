---
id: SPEC-CHAT-STATE-REDIS-001
plan_version: 0.1.0
spec_version: 0.1.0
created: 2026-05-20
methodology: DDD (ANALYZE-PRESERVE-IMPROVE)
target_branch: feature/chat-state-redis
---

# Implementation Plan — SPEC-CHAT-STATE-REDIS-001 v0.1.0

> **Plan HISTORY**:
> - 2026-05-20 (v0.1.0): 초안 — 4 REQ (cursor redis 화 / impression dedupe redis 화 / fail-open / 3환경 단일 게이트) plan. OQ-1~OQ-10 해소.
>
> **Scope guard**: WHAT/WHY는 spec.md에서 잠긴 상태. plan.md는 **HOW**만 결정한다. SPEC 에 잠긴 4 REQ 가 본 plan 의 입력. 본 plan 이 추가로 잠그는 결정은 SPEC §Open Questions (OQ-1 ~ OQ-10) 해소만이다.

> **Methodology**: **DDD (ANALYZE-PRESERVE-IMPROVE)**. 두 변경 모두 *기존 동작* 표면(`respond.py` 의 module-global dict 4개 접근 지점)을 건드린다. 변경 전에 현 동작(cursor 정확한 advance 값, dedupe set 정확한 멤버십 의미, id-less candidate 의 fresh 리스트 append 보존)을 capture 하는 characterization test 를 먼저 박고, PRESERVE 단계에서 byte-identical 보존(id-less 케이스, `is_fresh_search` 의미, fail-open 시 0/False fallback)을 보장한 뒤 IMPROVE 로 헬퍼 호출로 교체한다. 신규 모듈 `app/infrastructure/cache/chat_state.py` 와 redis pool lifespan 은 NEW 표면이지만 **사용 site 에서의 의미적 동작은 byte-identical**.

> **HARD prerequisite**: 본 plan 은 새 env var 1개(`REDIS_URL`) 외 추가 env / migration / 외부 서비스 의존을 추가하지 않는다. dev-ai 기존 redis 컨테이너(Langfuse v3 self-host) 그대로 재사용 — 새 컨테이너 생성 / 새 서비스 배포 없음.

---

## 0. Assumption Audit

| # | Assumption | Confidence | Risk if wrong |
|---|---|---|---|
| A1 | `app/agents/tools/respond.py` 의 `send_hybrid_batch` 안 cursor read/write 는 정확히 2 지점이고, `_log_delivered_impressions` 안 dedupe 는 정확히 4 지점(`pop` / `setdefault` / `seen` 검사 / `seen.add`). | High (직접 인스펙션 — spec.md Architecture Snapshot) | site 수가 다르면 plan §1 의 교체 라인 수 조정 — 단위 테스트가 신규 라인 cover. |
| A2 | dev-ai 의 redis 컨테이너는 Langfuse v3 self-host 일부로 이미 docker-compose 에 정의되어 있고, `kikoai-ai_app-net` 안에서 `redis:6379` 호스트명 도달 가능, `REDIS_AUTH` 인증 설정 완료. | High (memory: SPEC-OBSERVABILITY-002 dev-ai 풀스택 self-host) | host/port/auth 다르면 prod env 만 조정 — 코드 변경 없음. |
| A3 | dev-ai redis 의 maxmemory-policy 가 `allkeys-lru` 또는 `noeviction` 중 하나(Langfuse 가 의도적으로 set). 어느 쪽이든 kiko 키는 TTL 7d / 24h 안에서 자연 만료 — eviction 으로 sliently 사라져도 fail-open 으로 무영향. | Medium (운영 설정 인스펙션 필요) | `noeviction` + redis 메모리 풀 시 새 SET 명령 거부 → fail-open 으로 swallow + log. 동작 정상. |
| A4 | `_log_delivered_impressions` caller 는 `is_fresh_search: bool` 파라미터를 명시적으로 전달(offset==0 일 때 True). | High (코드 직접 확인 가능 — `respond.py` 안의 `send_hybrid_batch`) | caller 가 다른 패턴이면 헬퍼 inside 에서 offset 추론 — 다만 SPEC 은 caller 측에서 결정함을 lock. |
| A5 | python `redis>=5.0` 의 `redis.asyncio.Redis.from_url` 가 `redis://:auth@host:port/db` URL 을 표준 파싱(db 번호 포함). | High (redis-py 공식 문서) | URL 파싱 실패 시 lifespan warm 에서 즉시 fail-open + 명시적 ERROR 로그. |
| A6 | `fakeredis>=2.0` 가 본 SPEC 에서 쓰는 모든 명령(`GET`/`SET`/`SETEX`/`DEL`/`SADD`/`SISMEMBER`/`EXPIRE`/`TTL`/`PING`/`FLUSHDB`) 을 완전히 시뮬레이션. | High (fakeredis 공식 지원 매트릭스) | 특정 명령 불완전 시 단위 테스트가 즉시 fail — plan §2 가 대안(real redis container fixture) 검토. |
| A7 | `app/main.py` 의 lifespan context manager 가 이미 다른 클라이언트(DB pool, messenger adapter)의 startup warm + shutdown close 패턴을 갖고 있어 redis pool 도 같은 자리에 추가 가능. | High (CLAUDE.md 의 `lifespan (DB 클라이언트 워밍업 + messenger adapter + setWebhook)` 언급) | lifespan 구조 다르면 startup event 위치 조정. |
| A8 | `reset_card_batch_cursor_for_tests` 함수의 caller 는 본 SPEC 의 신규 테스트 + 기존 respond 관련 테스트만이고 외부 패키지 caller 없음. | Medium (grep 필요 — UX-T01) | 외부 caller 발견 시 모두 새 패턴(fakeredis fixture or `clear_logged`) 마이그레이션. |
| A9 | aws-infra 리포(`/Users/hansangho/Desktop/aws-infra/kiko-ai-servers/portal-ai/`) 의 docker-compose 가 `REDIS_URL` 환경변수를 받을 수 있는 구조(env_file 또는 environment block). | High (memory: dev-ai Telegram setup 패턴) | tracked infra .env 가 키를 안 가지면 재배포 시 회귀 — UX-T11 에 명시. |
| A10 | dev-ai redis 컨테이너의 DB 0 는 Langfuse v3 가 ClickHouse ingestion queue 로 사용 중이고, DB 1 은 비어있음(즉시 kiko 가 사용 가능). | Medium (직접 확인 필요 — UX-T01) | DB 1 이 다른 용도로 쓰이고 있다면 DB 2 또는 명시적 prefix 사용 — env URL 만 조정. |

**Critical surfacing**: A3 (maxmemory-policy), A8 (외부 caller), A10 (DB 1 점유 여부) — 코드 시작 전 1차 인스펙션 필요. 나머지는 unit-level characterization 으로 검증.

---

## 1. Module Structure & Public Surface

### 1.1 `app/infrastructure/cache/__init__.py` 신규

```python
"""Infrastructure cache package — Redis-backed chat state.

SPEC-CHAT-STATE-REDIS-001.
"""
```

(빈 패키지 마커. 추가 export 없음.)

### 1.2 `app/infrastructure/cache/chat_state.py` 신규 (REQ-CHAT-STATE-001 + REQ-CHAT-STATE-002 + REQ-CHAT-STATE-003)

```python
"""Redis-backed chat state — pager cursor + impression dedupe.

SPEC-CHAT-STATE-REDIS-001 REQ-CHAT-STATE-001..004.

Two invariants protected:
- `kiko:cursor:{chat_id}` (TTL 24h, int value): next-page start index for
  "더보기 / More" pager. Replaces `_CARD_BATCH_CURSOR` module-global dict.
- `kiko:imp:{chat_id}` (TTL 7d, SET of product_ids): per-chat impression
  dedupe set. Replaces `_LOGGED_IMPRESSION_IDS` module-global dict.

All helpers fail-open: redis unavailability NEVER blocks card delivery.

Single Redis pool instance (module-level, lazy init). Created on first call
or via `warm_pool()` from `app.main` lifespan startup.
"""

from __future__ import annotations

import logging
from typing import Final

from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)

# @MX:ANCHOR: [AUTO] sole owner of redis chat-state surface — every cursor /
#   impression-dedupe operation goes through these 5 helpers. No direct
#   redis-py calls from `respond.py` or any other caller.
# @MX:REASON: SPEC-CHAT-STATE-REDIS-001 REQ-CHAT-STATE-003 fail-open uniformity
#   — caller passes through without try/except; swallow is centralized here.
# @MX:SPEC: SPEC-CHAT-STATE-REDIS-001

_CURSOR_TTL_SECONDS: Final[int] = 86_400   # 24h
_IMP_TTL_SECONDS: Final[int] = 604_800     # 7d

_pool: Redis | None = None


def _cursor_key(chat_id: int) -> str:
    return f"kiko:cursor:{int(chat_id)}"


def _imp_key(chat_id: int) -> str:
    return f"kiko:imp:{int(chat_id)}"


def _get_client() -> Redis | None:
    """Return the module-level singleton client, lazy-creating it on first call.

    Fail-open: returns None on any creation error.
    """
    global _pool
    if _pool is not None:
        return _pool
    try:
        # `decode_responses=True` keeps cursor int parsing simple and SET
        # membership returns str (matching product_id str dtype).
        _pool = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("redis pool create failed: %s", type(exc).__name__)
        _pool = None
    return _pool


async def warm_pool() -> bool:
    """Lifespan startup hook — try a PING, return True on success.

    Fail-open: logs at INFO and returns False on failure. The pool remains
    None and subsequent helper calls re-attempt via `_get_client` lazily.
    """
    client = _get_client()
    if client is None:
        logger.info("redis warm skipped (fail-open): pool create returned None")
        return False
    try:
        ok = await client.ping()
        if ok:
            logger.info("redis pool warmed (url=%s)", _mask_url(settings.REDIS_URL))
            return True
        logger.info("redis warm skipped (fail-open): PING returned falsy")
        return False
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.info("redis warm skipped (fail-open): %s", type(exc).__name__)
        return False


async def close_pool() -> None:
    """Lifespan shutdown hook — close the pool. Fail-open."""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.aclose()
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("redis pool close failed: %s", type(exc).__name__)
    _pool = None


def _mask_url(url: str) -> str:
    """Mask the auth portion of a redis URL for logging."""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    auth, host = rest.split("@", 1)
    return f"{scheme}://****@{host}"


# --- Pager cursor (REQ-CHAT-STATE-001) ---


async def get_cursor(chat_id: int) -> int:
    """Return the next-page start index for `chat_id`. Fail-open → 0."""
    client = _get_client()
    if client is None:
        return 0
    try:
        raw = await client.get(_cursor_key(chat_id))
        if raw is None:
            return 0
        return int(raw)
    except (RedisError, ValueError, Exception) as exc:  # noqa: BLE001
        logger.debug("get_cursor fail-open chat=%s: %s", chat_id, type(exc).__name__)
        return 0


async def set_cursor(chat_id: int, n: int) -> None:
    """Set the next-page start index for `chat_id` with TTL 24h. Fail-open."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.set(_cursor_key(chat_id), int(n), ex=_CURSOR_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("set_cursor fail-open chat=%s: %s", chat_id, type(exc).__name__)


# --- Impression dedupe (REQ-CHAT-STATE-002) ---


async def is_logged(chat_id: int, product_id: str) -> bool:
    """Return True iff `product_id` is already in the dedupe set. Fail-open → False."""
    if not product_id:
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        return bool(await client.sismember(_imp_key(chat_id), str(product_id)))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("is_logged fail-open chat=%s pid=%s: %s", chat_id, product_id, type(exc).__name__)
        return False


async def mark_logged(chat_id: int, product_id: str) -> None:
    """Add `product_id` to the dedupe set + extend TTL 7d. Fail-open."""
    if not product_id:
        return
    client = _get_client()
    if client is None:
        return
    key = _imp_key(chat_id)
    try:
        await client.sadd(key, str(product_id))
        # EXPIRE on every SADD — cheap, idempotent. (Alternative: EXPIRE NX
        # via redis 7+ to avoid re-extending, but the cost is negligible.)
        await client.expire(key, _IMP_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("mark_logged fail-open chat=%s pid=%s: %s", chat_id, product_id, type(exc).__name__)


async def clear_logged(chat_id: int) -> None:
    """Drop the dedupe set for `chat_id` (new search → fresh trace binding). Fail-open."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.delete(_imp_key(chat_id))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("clear_logged fail-open chat=%s: %s", chat_id, type(exc).__name__)
```

**Resolved OQ-1** (lazy 초기화): module-level `_pool` + `_get_client` lazy. `warm_pool()` 은 lifespan 가 호출하지만 실패해도 무영향 — 헬퍼들이 매 호출 마다 `_get_client` 로 폴백 가능.

**Resolved OQ-2** (set 명령): `client.set(key, value, ex=TTL)` 사용. redis-py 의 high-level API — `SETEX` 동치. plan §1 에서 lock.

**Resolved OQ-3** (EXPIRE 빈도): 매 `SADD` 마다 `EXPIRE` — 단순. NX 옵션 회피(redis 7 의존성).

**Resolved OQ-6** (chat_id 타입): `int`. 키 생성 시 `int(chat_id)` 변환으로 caller 의 str/int 혼용 흡수.

**Resolved OQ-8** (REDIS_URL default): `"redis://localhost:6379/1"` — 로컬 docker-compose 와 일치.

### 1.3 `app/core/config.py` 변경 (REQ-CHAT-STATE-004)

```python
# 기존 Settings 클래스에 추가
class Settings(BaseSettings):
    # ... 기존 필드들 ...

    # SPEC-CHAT-STATE-REDIS-001 REQ-CHAT-STATE-004
    REDIS_URL: str = "redis://localhost:6379/1"
```

### 1.4 `app/main.py` 변경 (REQ-CHAT-STATE-003 + REQ-CHAT-STATE-004 lifespan)

```python
# 기존 lifespan async context manager 안에 추가
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... 기존 startup (DB pool, messenger adapter, setWebhook) ...

    # NEW (SPEC-CHAT-STATE-REDIS-001)
    from app.infrastructure.cache import chat_state
    await chat_state.warm_pool()  # fail-open inside

    yield

    # ... 기존 shutdown ...

    # NEW
    await chat_state.close_pool()
```

**Resolved OQ-7** (warm 실패 로그 레벨): `logger.info` — 정보성, 의도된 fail-open 임을 명시. WARN/ERROR 회피(noisy).

### 1.5 `app/agents/tools/respond.py` 변경 (REQ-CHAT-STATE-001 + REQ-CHAT-STATE-002)

#### Module-global 제거

```python
# 삭제 (line 70-101 영역):
# _CARD_BATCH_CURSOR: dict[int, int] = {}
# _LOGGED_IMPRESSION_IDS: dict[int, set[str]] = {}
# def reset_card_batch_cursor_for_tests() -> None: ...
```

대신 import:

```python
from app.infrastructure.cache import chat_state
```

#### Cursor 교체 (`send_hybrid_batch`)

기존(추정):
```python
# write 지점
_CARD_BATCH_CURSOR[chat_id] = next_offset

# read 지점 (cards:more callback path)
offset = _CARD_BATCH_CURSOR.get(chat_id, 0)
```

교체:
```python
# write
await chat_state.set_cursor(chat_id, next_offset)

# read
offset = await chat_state.get_cursor(chat_id)  # fail-open → 0
```

#### Impression dedupe 교체 (`_log_delivered_impressions`)

기존(line 136-159 영역):
```python
if is_fresh_search:
    _LOGGED_IMPRESSION_IDS.pop(int(chat_id), None)
seen = _LOGGED_IMPRESSION_IDS.setdefault(int(chat_id), set())
fresh: list[Any] = []
for c in batch:
    pid = _product_id_of(c)
    if pid is None:
        fresh.append(c)
        continue
    if pid in seen:
        continue
    seen.add(pid)
    fresh.append(c)
```

교체:
```python
if is_fresh_search:
    await chat_state.clear_logged(int(chat_id))
fresh: list[Any] = []
for c in batch:
    pid = _product_id_of(c)
    if pid is None:
        # id-less candidates: pass through (log_impressions skips them itself).
        fresh.append(c)
        continue
    if await chat_state.is_logged(int(chat_id), str(pid)):
        continue
    await chat_state.mark_logged(int(chat_id), str(pid))
    fresh.append(c)
```

**byte-identical 의미 보존**:
- id-less candidate → fresh.append (unchanged).
- 중복 product_id → skip (unchanged).
- `is_fresh_search=True` → 사전 set DEL (unchanged 의미; 구현만 dict.pop → redis DEL).
- Redis 실패 시 fail-open → `is_logged` False → 중복 INSERT 허용 (기존 dict 가 없을 때와 동일 행동, functional 무결).

**Resolved OQ-4** (`is_fresh_search` 판정 위치): caller(`send_hybrid_batch`) 측에서 `is_fresh_search = (offset == 0)` 패턴. SPEC §Background 와 기존 코드 주석 일관 — 변경 없음.

#### 테스트 hook 제거

`reset_card_batch_cursor_for_tests` 제거. 새 테스트는:
- `tests/conftest.py` 또는 `tests/test_agents/tools/conftest.py` 에 fakeredis fixture 정의
- fixture 가 `chat_state._pool` 을 fakeredis 인스턴스로 monkeypatch
- 매 테스트 케이스마다 새 fakeredis(function scope) — 자동 격리

### 1.6 `pyproject.toml` 변경

```toml
[project]
dependencies = [
  # ... 기존 ...
  "redis>=5.0",
]

[dependency-groups]
dev = [
  # ... 기존 ...
  "fakeredis>=2.0",
]
```

### 1.7 로컬 `docker-compose.yml` 변경 (REQ-CHAT-STATE-004 로컬)

```yaml
services:
  redis:
    image: redis:7-alpine
    container_name: kiko-ai-redis
    ports:
      - "6379:6379"
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3

  ai-server:
    # ... 기존 ...
    depends_on:
      redis:
        condition: service_healthy
    environment:
      # ... 기존 ...
      REDIS_URL: redis://redis:6379/1
```

**Resolved OQ-9** (redis 이미지 버전): `redis:7-alpine` — Langfuse v3 의 prod 컨테이너 버전과 호환(7.x 라인). prod 이 다른 minor 면 dev-ai 의 정확한 버전(`docker exec redis redis-cli INFO server | grep redis_version`) 확인 후 일치.

### 1.8 aws-infra docker-compose 변경 (REQ-CHAT-STATE-004 prod)

`aws-infra/kiko-ai-servers/portal-ai/docker-compose.yml` (또는 동치) 의 `ai-server` 서비스에:

```yaml
services:
  ai-server:
    # ... 기존 ...
    environment:
      # ... 기존 ...
      REDIS_URL: redis://:${REDIS_AUTH}@redis:6379/1
```

`/home/ec2-user/.env` 에 `REDIS_AUTH` 가 이미 Langfuse 용으로 설정되어 있음 (확인 필요 — UX-T01). 같은 값 재사용.

---

## 2. Test Strategy

### 2.1 `tests/test_infrastructure/cache/test_chat_state.py` (NEW)

```python
"""SPEC-CHAT-STATE-REDIS-001 unit tests for chat_state helpers."""

from __future__ import annotations

import asyncio
import logging

import fakeredis.aioredis
import pytest

from app.infrastructure.cache import chat_state


@pytest.fixture
async def fake_redis(monkeypatch):
    """Per-test fakeredis instance bound to chat_state._pool."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_state, "_pool", client)
    yield client
    await client.aclose()
    monkeypatch.setattr(chat_state, "_pool", None)


# --- REQ-CHAT-STATE-001 cursor happy path ---

async def test_set_cursor_then_get_returns_value(fake_redis):
    await chat_state.set_cursor(chat_id=42, n=5)
    assert await chat_state.get_cursor(42) == 5


async def test_get_cursor_unset_returns_zero(fake_redis):
    assert await chat_state.get_cursor(99) == 0


async def test_set_cursor_sets_ttl_24h(fake_redis):
    await chat_state.set_cursor(chat_id=42, n=10)
    ttl = await fake_redis.ttl("kiko:cursor:42")
    assert 0 < ttl <= 86_400


# --- REQ-CHAT-STATE-002 impression dedupe happy path ---

async def test_mark_logged_then_is_logged_true(fake_redis):
    await chat_state.mark_logged(42, "prod-A")
    assert await chat_state.is_logged(42, "prod-A") is True
    assert await chat_state.is_logged(42, "prod-B") is False


async def test_clear_logged_drops_set(fake_redis):
    await chat_state.mark_logged(42, "prod-A")
    await chat_state.mark_logged(42, "prod-B")
    await chat_state.clear_logged(42)
    assert await chat_state.is_logged(42, "prod-A") is False
    assert await chat_state.is_logged(42, "prod-B") is False


async def test_mark_logged_sets_ttl_7d(fake_redis):
    await chat_state.mark_logged(42, "prod-A")
    ttl = await fake_redis.ttl("kiko:imp:42")
    assert 0 < ttl <= 604_800


async def test_is_logged_empty_pid_returns_false(fake_redis):
    assert await chat_state.is_logged(42, "") is False
    # mark_logged with empty pid: no-op, no key created
    await chat_state.mark_logged(42, "")
    assert await fake_redis.exists("kiko:imp:42") == 0


# --- REQ-CHAT-STATE-003 fail-open (no fakeredis — direct raising mock) ---

class _RaisingClient:
    async def get(self, *_a, **_k): raise ConnectionError("boom")
    async def set(self, *_a, **_k): raise ConnectionError("boom")
    async def sismember(self, *_a, **_k): raise ConnectionError("boom")
    async def sadd(self, *_a, **_k): raise ConnectionError("boom")
    async def expire(self, *_a, **_k): raise ConnectionError("boom")
    async def delete(self, *_a, **_k): raise ConnectionError("boom")


@pytest.fixture
def raising_redis(monkeypatch):
    monkeypatch.setattr(chat_state, "_pool", _RaisingClient())
    yield
    monkeypatch.setattr(chat_state, "_pool", None)


async def test_get_cursor_fail_open_returns_zero(raising_redis, caplog):
    caplog.set_level(logging.DEBUG)
    assert await chat_state.get_cursor(42) == 0
    debug_records = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("get_cursor fail-open" in r.message for r in debug_records)
    assert all(r.levelname not in ("WARNING", "ERROR") for r in caplog.records)


async def test_set_cursor_fail_open_swallow(raising_redis, caplog):
    caplog.set_level(logging.DEBUG)
    await chat_state.set_cursor(42, 5)  # no raise
    assert any(r.levelname == "DEBUG" and "set_cursor fail-open" in r.message for r in caplog.records)


async def test_is_logged_fail_open_returns_false(raising_redis):
    assert await chat_state.is_logged(42, "prod-A") is False


async def test_mark_logged_fail_open_swallow(raising_redis):
    await chat_state.mark_logged(42, "prod-A")  # no raise


async def test_clear_logged_fail_open_swallow(raising_redis):
    await chat_state.clear_logged(42)  # no raise


# --- pool init failure ---

async def test_warm_pool_returns_false_on_unreachable(monkeypatch):
    monkeypatch.setattr(chat_state, "_pool", None)
    monkeypatch.setattr(chat_state.settings, "REDIS_URL", "redis://nonexistent-host:6379/0")
    # warm_pool MUST NOT raise — returns False, lifespan startup proceeds
    result = await chat_state.warm_pool()
    assert result is False
    # subsequent helper calls also fail-open
    assert await chat_state.get_cursor(42) == 0
```

### 2.2 `tests/test_agents/tools/test_respond_redis_integration.py` (NEW)

```python
"""SPEC-CHAT-STATE-REDIS-001 integration: respond.py exercises chat_state helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from app.agents.tools import respond
from app.infrastructure.cache import chat_state


@pytest.fixture
async def fake_redis(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(chat_state, "_pool", client)
    yield client
    await client.aclose()
    monkeypatch.setattr(chat_state, "_pool", None)


# --- Cursor integration ---

async def test_send_hybrid_batch_advances_cursor_in_redis(fake_redis):
    """offset=0 호출 후 redis cursor 가 다음 페이지 시작 인덱스로 set."""
    # ... fixture: minimal state with 10 candidates, batch_size=5 ...
    # await respond.send_hybrid_batch(state, offset=0, ...)
    raw = await fake_redis.get("kiko:cursor:42")
    assert raw is not None
    assert int(raw) == 5  # next_offset


async def test_cards_more_reads_cursor_from_redis(fake_redis):
    """offset=None 호출이 redis cursor 를 읽어 그 위치부터 batch 발사."""
    await chat_state.set_cursor(42, 5)
    # await respond.send_hybrid_batch(state, offset=None, ...)
    # assert: 5번째 인덱스부터 시작했는지 sent batch 검사
    ...


# --- Impression dedupe integration ---

async def test_log_delivered_impressions_dedupes_within_chat(fake_redis):
    """같은 batch 두 번 발사 → log_impressions INSERT 1회 (두 번째는 dedupe)."""
    with patch("app.channels.implicit_feedback.log_impressions", new=AsyncMock()) as mock_log:
        batch = [{"id": f"prod-{i}", ...} for i in range(5)]
        await respond._log_delivered_impressions(
            chat_id=42, sess=..., batch=batch, is_fresh_search=True
        )
        assert mock_log.await_count == 1  # 5건 INSERT
        # 두 번째 호출 — 같은 batch, is_fresh_search=False (cards:more 시뮬레이션)
        await respond._log_delivered_impressions(
            chat_id=42, sess=..., batch=batch, is_fresh_search=False
        )
        # mock_log 가 다시 불렸지만 fresh 길이 0 — assert fresh=[]
        assert mock_log.await_args_list[-1].args[2] == []


async def test_fresh_search_clears_dedupe(fake_redis):
    """is_fresh_search=True → redis SET DEL 후 새로 등록."""
    await chat_state.mark_logged(42, "prod-A")
    with patch("app.channels.implicit_feedback.log_impressions", new=AsyncMock()) as mock_log:
        batch = [{"id": "prod-A", ...}]
        await respond._log_delivered_impressions(
            chat_id=42, sess=..., batch=batch, is_fresh_search=True
        )
        # 같은 product_id 라도 새 trace 에 다시 logged
        assert mock_log.await_args_list[-1].args[2] == batch


async def test_id_less_candidate_passes_through(fake_redis):
    """product_id=None candidate 는 dedupe 우회, fresh 리스트에 그대로."""
    with patch("app.channels.implicit_feedback.log_impressions", new=AsyncMock()) as mock_log:
        batch = [
            {"id": None, ...},        # id-less
            {"id": "prod-A", ...},
        ]
        await respond._log_delivered_impressions(
            chat_id=42, sess=..., batch=batch, is_fresh_search=True
        )
        fresh = mock_log.await_args_list[-1].args[2]
        assert len(fresh) == 2  # id-less + prod-A 모두 통과
        # SET 에는 prod-A 만
        assert await fake_redis.sismember("kiko:imp:42", "prod-A")


async def test_redis_down_does_not_block_card_delivery(monkeypatch):
    """fail-open: redis 다운 시 log_impressions 그대로 호출."""
    monkeypatch.setattr(chat_state, "_pool", _RaisingClient())  # 모든 명령 raise
    with patch("app.channels.implicit_feedback.log_impressions", new=AsyncMock()) as mock_log:
        batch = [{"id": "prod-A", ...}, {"id": "prod-B", ...}]
        # 예외 발생 없이 정상 완료
        await respond._log_delivered_impressions(
            chat_id=42, sess=..., batch=batch, is_fresh_search=True
        )
        # is_logged 가 False 반환 → 두 candidate 모두 fresh
        fresh = mock_log.await_args_list[-1].args[2]
        assert len(fresh) == 2
```

### 2.3 회귀 grep test (NEW, conftest 또는 별도 파일)

```python
def test_no_module_global_chat_state_dicts():
    """SPEC-CHAT-STATE-REDIS-001: module-global dicts must be fully removed."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    forbidden = ["_CARD_BATCH_CURSOR", "_LOGGED_IMPRESSION_IDS", "reset_card_batch_cursor_for_tests"]
    offenders: list[str] = []
    for path in root.glob("app/**/*.py"):
        src = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in src:
                offenders.append(f"{path.relative_to(root)}: {token}")
    assert offenders == [], f"Forbidden module-global tokens still present: {offenders}"
```

---

## 3. Sequencing (Run Phase tasks)

| Task | Description | Acceptance |
|---|---|---|
| CSR-T01 | Inspect `app/agents/tools/respond.py` (cursor + dedupe site 정확한 line 수 — A1), aws-infra docker-compose redis 환경 (A2, A9), dev-ai redis DB 1 점유 여부 (A10), `reset_card_batch_cursor_for_tests` 외부 caller (A8), maxmemory-policy (A3). 결과는 `progress.md` 에 문서화. | Inspection notes committed. |
| CSR-T02 | Characterization: 현 cursor advance 정확한 값 + 현 dedupe 동작(id-less / 같은 batch 재호출 / is_fresh_search) snapshot test 작성. 동작 변경 없음 — baseline. | Baseline tests committed, green on current code. |
| CSR-T03 | `app/infrastructure/cache/__init__.py` + `app/infrastructure/cache/chat_state.py` 작성 (5 헬퍼 + warm_pool/close_pool + lazy `_get_client`). | 모듈 작성 완료. |
| CSR-T04 | `tests/test_infrastructure/cache/test_chat_state.py` 작성 (~12 케이스 — happy/fail-open/TTL/warm). | 모든 케이스 green. |
| CSR-T05 | `app/core/config.py` 에 `REDIS_URL` 필드 + `pyproject.toml` 에 `redis>=5.0` / `fakeredis>=2.0` 추가. | `uv sync` 성공. |
| CSR-T06 | `app/main.py` lifespan 에 `warm_pool` / `close_pool` 추가. fail-open 검증. | FastAPI TestClient startup 정상. |
| CSR-T07 | `app/agents/tools/respond.py` 교체: module-global dict 2개 + reset 함수 제거, cursor 2 지점 헬퍼로, dedupe 4 지점 헬퍼로. | grep 회귀 test green, 기존 respond 동작 byte-identical. |
| CSR-T08 | `tests/test_agents/tools/test_respond_redis_integration.py` 작성 (~6 케이스). | 모든 케이스 green. |
| CSR-T09 | 회귀 grep test (forbidden tokens 없음). | Green. |
| CSR-T10 | 로컬 docker-compose 에 redis 서비스 추가 + `depends_on`. | `docker compose up -d` → 정상 기동, `docker exec redis redis-cli ping` PONG. |
| CSR-T11 | aws-infra docker-compose 의 `ai-server` env 에 `REDIS_URL` 추가, 커밋. (별도 리포 — separate PR). | aws-infra 커밋 머지. |
| CSR-T12 | Full regression: `uv run pytest -q`. | All existing tests pass. |
| CSR-T13 | `uv run ruff check . && uv run ruff format --check .`. | Green. |
| CSR-T14 | 로컬 manual smoke (DoD 항목): docker-compose up → 봇 시나리오 → redis CLI 키 확인. | Recorded in `progress.md`. |
| CSR-T15 | dev-ai prod smoke (DoD 항목): 배포 → 1 사용자 시나리오 → `ai.card_impression` 중복 row 없음 확인 + redis CLI 키 확인. | Recorded in `progress.md`. |
| CSR-T16 | CLAUDE.md 갱신 (3 지점: 핵심 파일 표 / respond.py 설명 / 환경 변수). | CLAUDE.md diff committed. |
| CSR-T17 | acceptance.md final mapping 업데이트. | All P1 REQ rows show automated test paths. |

Priority order: CSR-T01 (inspection) → CSR-T02 (baseline) → CSR-T03/T05 (모듈+config) 병렬 → CSR-T04/T06 병렬 → CSR-T07 (respond 교체) → CSR-T08/T09 병렬 → CSR-T10/T11 (인프라) 병렬 → CSR-T12/T13 → CSR-T14 → CSR-T15 → CSR-T16/T17.

---

## 4. Risk Mitigation Details

### R1 — Redis down blocks cards

- Mitigation: 5 헬퍼 전부 try/except 로 wrap, caller 측은 await 만. 단위 테스트 5개 (`_RaisingClient` fixture) 가 fail-open 강제.
- Monitor: post-merge, `docker logs ai-server | grep "fail-open"` 빈도. 지속 발생 시 redis 자체 운영 이슈 — 별도 ticket.

### R2 — Langfuse key collision

- Mitigation: (a) `REDIS_URL` 의 `/1` 명시. (b) 모든 키 `kiko:` prefix. (c) lifespan warm 시 `_mask_url` 로 db 번호 로그(`url=redis://****@redis:6379/1`).
- Verification: dev-ai 배포 후 `docker exec redis redis-cli -n 0 KEYS 'kiko:*'` 가 빈 set, `-n 1 KEYS 'kiko:*'` 가 사용자 활동 후 채워짐.

### R3 — Redis memory pressure

- Mitigation: TTL 24h / 7d + 새 검색 시 DEL. 활성 1만 chat × 100 멤버 × 50바이트 ≈ 50MB — dev-ai redis 의 maxmemory 안에서 안전. eviction 발생 시 fail-open으로 무영향.

### R4 — fakeredis behavior divergence

- Mitigation: SPEC 의 5 명령(`GET`/`SET ex=`/`SISMEMBER`/`SADD`/`EXPIRE`/`DELETE`/`TTL`)은 모두 fakeredis 공식 지원. TTL 검증은 "양수 반환"만 assert.
- Backup: 만약 fakeredis 한계 발견 시 `redis:7-alpine` testcontainers 도입 — plan §1 의 헬퍼 시그니처는 그대로 호환.

### R5 — REDIS_URL missing in prod

- Mitigation: lifespan warm INFO 로그가 prod 배포 직후 redis 도달 가능성 확인. dev-ai 운영 checklist (UX-T11) 에 `REDIS_URL` 환경변수 명시 추가. aws-infra 리포 docker-compose 추적.

### R6 — `reset_card_batch_cursor_for_tests` external callers

- Mitigation: CSR-T01 에서 grep 으로 caller 검색. 발견된 caller 는 모두 새 fakeredis fixture 또는 `clear_logged` 호출로 마이그레이션.

### R7 — Pool singleton race

- Mitigation: asyncio 단일 event loop 가정. `_pool is None` 체크 후 `Redis.from_url` 호출 — GIL + asyncio 단일 스레드 안에서 race 없음.

### R8 — TTL 24h vs paging session

- Mitigation: 24h 안전 마진. 사용자 데이터 부족 입증 시 SPEC version bump.

### R9 — fakeredis maintenance

- Mitigation: pin `fakeredis>=2.0` — 2026 기준 active.

### R10 — id-less dedupe bypass regression

- Mitigation: `is_logged` / `mark_logged` 시그니처 `product_id: str` 명시 + 함수 내부 `if not product_id: return False/None`. caller(`_log_delivered_impressions`) 의 `if pid is None: fresh.append(c); continue` 가드. 단위 테스트 `test_is_logged_empty_pid_returns_false` + 통합 `test_id_less_candidate_passes_through`.

### R11 — Redis maxmemory eviction

- Mitigation: kiko 키 사이즈 작음(R3). eviction 발생 시 fail-open. 운영 모니터링 `INFO memory` 별도 SPEC.

---

## 5. Cutover

1. Implement on `feature/chat-state-redis` branch.
2. Local `uv run pytest -q` green + `docker compose up -d` smoke (CSR-T14).
3. aws-infra PR — `ai-server` 환경변수 `REDIS_URL` 추가 (separate from app PR — 인프라 변경 분리).
4. ai PR → review (focus: fail-open 단위 테스트, grep 회귀, lifespan warm 동작, byte-identical 의미 보존, manual smoke 결과).
5. aws-infra PR 머지 → dev-ai 재배포 (env 적용).
6. ai PR 머지 → dev-ai 재배포.
7. 24h observation:
   - `docker logs ai-server | grep "redis pool warmed"` — 시작 직후 1회 확인.
   - `docker logs ai-server | grep "fail-open"` — 지속 0 또는 매우 낮은 빈도.
   - `docker exec redis redis-cli -a $REDIS_AUTH -n 1 KEYS 'kiko:*' | wc -l` — 사용자 활동 비례로 증가.
   - `SELECT product_id, chat_id, COUNT(*) FROM ai.card_impression WHERE created_at > NOW() - INTERVAL '24 hours' GROUP BY 1, 2 HAVING COUNT(*) > 1` — 단일 worker 환경에서 0건 확인 (멀티 worker 미도입 단계).
   - 사용자 베타 채널에 "더보기" 끊김 / 컨테이너 재시작 후 paging 재개 등 피드백 모니터링.

---

## 6. Out-of-Plan Items

- `_NODE_MARKERS` Redis 이전 — 별도 SPEC (필요 시).
- `session_pg.py` / `taste_profile_pg.py` Redis 마이그레이션 — 별도 SPEC (더 큰 작업).
- Redis 클러스터링 / sentinel / ElastiCache — 별도 SPEC.
- 새 env var (`REDIS_URL` 외) — 본 plan 미포함.
- Langfuse 새 span 추가 — fail-open DEBUG 로그로 충분.
- Multi-worker uvicorn 실제 도입 — 본 plan 은 prerequisite 만 충족. 실제 워커 수 증가는 별도 SPEC + 부하 테스트.
- Redis 메트릭 / Grafana 보드 — 별도 SPEC.
- prod redis 의 maxmemory-policy 변경 — 별도 운영 ticket.
