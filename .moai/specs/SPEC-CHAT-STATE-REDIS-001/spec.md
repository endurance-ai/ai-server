---
id: SPEC-CHAT-STATE-REDIS-001
version: 0.1.0
status: draft
created: 2026-05-20
updated: 2026-05-20
author: hchsa77@gmail.com
priority: P1
issue_number: null
labels: [chat-state, redis, infrastructure, respond, impression-dedupe, pager-cursor, multi-worker-ready]
---

# SPEC-CHAT-STATE-REDIS-001: Chat-State Externalization to Redis — Pager Cursor + Impression Dedupe

## HISTORY

- 2026-05-20 (v0.1.0): 초안 작성. `app/agents/tools/respond.py` 안에 process-local Python dict 두 개(`_CARD_BATCH_CURSOR` line 77 — 더보기 페이저 cursor / `_LOGGED_IMPRESSION_IDS` line 95 — `ai.card_impression` 중복 INSERT 차단 dedupe set)가 누적 중. 둘 다 (a) 메모리 누수 — process 영구 누적, 컨테이너 OOM 으로만 강제 리셋, (b) 다음 분기 멀티-워커/멀티-인스턴스 스케일 시 깨짐 — 워커별 분리로 cursor 일관성 깨지고 dedupe 우회되어 `ai.card_impression` 중복 INSERT → Langfuse implicit feedback score 분모 부풀림, (c) 컨테이너 재시작 시 사용자 paging 상태 리셋. 해결: 두 dict 를 dev-ai 기존 redis 컨테이너(Langfuse v3 가 깐 것, `kikoai-ai_app-net` 동일망)의 DB 1(DB 0 는 Langfuse) 로 이전. 단일 환경변수 `REDIS_URL` 로 로컬/테스트/prod 3환경 제어. fail-open(redis 다운 시 추천은 계속 — cursor 못 읽으면 0, dedupe 못 하면 중복 INSERT 허용). 별 dict (`app/channels/pre_messages.py::_NODE_MARKERS`)는 이미 cap 64 + half-eviction LRU 처리되어 본 SPEC 미포함. session_pg / taste_profile_pg 의 PostgreSQL → Redis 마이그레이션은 별도 SPEC(더 큰 작업)로 분리. WHAT/WHY 만 정의 — HOW(헬퍼 함수 정확한 시그니처, redis pool 라이프사이클 hook, ctx 키 이름 등)는 `plan.md` 및 Run phase 에서 결정.

---

## Goal

`app/agents/tools/respond.py` 의 process-local chat-state 두 dict 를 외부 Redis 로 이전해 (1) process 메모리 누수를 닫고, (2) 다음 분기 멀티-워커/멀티-인스턴스 스케일 시 두 dict 가 워커별로 갈라져 발생할 결함(cursor 일관성 깨짐 → 사용자 동일 페이지 재노출, impression dedupe 우회 → Langfuse score 분모 부풀림)을 사전에 차단하며, (3) 컨테이너 재시작 후에도 사용자 paging 상태를 보존한다.

| chat-state | 현재 위치 (코드 갭) | 사용자 체감 결함 | 본 SPEC 의 처방 |
|---|---|---|---|
| 더보기 페이저 다음 페이지 시작 인덱스 | `app/agents/tools/respond.py:77` `_CARD_BATCH_CURSOR: dict[int, int]` (chat_id → next offset) | (a) 메모리 누수 (process 영구 누적) (b) 멀티-워커 시 워커별 cursor 분리 → "더보기" 탭에서 같은 페이지 재노출 (c) 재시작 시 paging 상태 초기화 | Redis 키 `kiko:cursor:{chat_id}` (TTL 24h) 로 이전 |
| Impression INSERT 중복 차단 dedupe set | `app/agents/tools/respond.py:95` `_LOGGED_IMPRESSION_IDS: dict[int, set[str]]` (chat_id → 이번 chat 에서 logged product_id set) | (a) 메모리 누수 (b) 멀티-워커 시 dedupe 우회 → `ai.card_impression` 중복 INSERT → Langfuse implicit feedback score 분모 부풀림 | Redis SET 키 `kiko:imp:{chat_id}` (TTL 7d, 새 검색 시 DEL) 로 이전 |

두 변경 모두:

1. **외부 토폴로지 무변경.** 새 서비스 없음 (기존 redis 컨테이너 재사용). 새 마이그레이션 없음. 새 LLM endpoint 없음. 새 그래프 노드 없음.
2. **Fail-open.** Redis 다운 / 명령 실패 시 추천 카드 발사는 그대로 진행. cursor 못 읽으면 `0`(첫 페이지부터 다시), dedupe 못 하면 중복 INSERT 허용(기존 dict 가 없을 때와 동일 행동), 모든 redis 호출은 try/except 래핑 + `logger.debug` 1줄.
3. **3환경 단일 게이트.** `REDIS_URL` 환경변수 한 개로 로컬(docker-compose 신규 redis)/테스트(`fakeredis`)/prod(dev-ai 기존 redis DB 1) 분기. flag 없음 (SPEC-AGENT-V2-CLEANUP-001 "no feature flags" 정책 일관).

이 SPEC 은 **WHAT** 과 **WHY** 만 정의한다. 정확한 헬퍼 시그니처, redis pool 의 lazy 초기화 방식, ctx 키 이름, `is_fresh_search` 의 정확한 판정 위치 — 모두 `plan.md` 와 Run phase 에서 결정한다.

---

## Background

### 결함 #1 — 더보기 페이저 cursor process-local (REQ-CHAT-STATE-001 의 동기)

`app/agents/tools/respond.py` line 70-77 에 정의된 dict:

```python
# In-memory "더보기 / More" pager cursor, keyed by chat_id. Module-global so it
# survives across webhook calls within the process (the original search turn
# sets it; a later `cards:more` tap reads + advances it). NOT persisted: a
# process restart simply restarts the pager from the first batch, which is
# harmless (last_results may not survive an in-memory store restart either).
# Avoiding a Session field keeps this change free of an Alembic migration
# (the PG session store uses an explicit column list, not field reflection).
_CARD_BATCH_CURSOR: dict[int, int] = {}
```

이 dict 는 `send_hybrid_batch` 가 album+summary 5장 발사 후 "다음 페이지 시작 인덱스"(예: 5, 10, 15…)를 저장하고, `cards:more` 콜백으로 `send_hybrid_batch(offset=None)` 호출 시 이 값을 읽어 그 위치부터 다음 batch 를 발사한다.

문제:

- **메모리 누수**: process 영구 누적. 컨테이너 OOM 강제 리셋만이 유일한 회수 경로. dev-ai EC2 t4g.large (8GB) 환경에서 사용자 수십만 명 누적되면 dict 가 1MB 이상으로 자라 컨테이너 안정성 위협.
- **멀티-워커 시 일관성 붕괴**: 현재는 uvicorn `--workers 1` 가정. 다음 분기 다중 워커 / 다중 인스턴스 도입 시 webhook payload 가 random worker 로 routing → 같은 chat_id 의 첫 검색이 worker A, "더보기" 탭이 worker B 로 가면 B 는 dict 가 비어있어 cursor=0 으로 처리 → 사용자가 똑같은 첫 5장을 또 봄. UX 신뢰도 즉각 손상.
- **재시작 reset**: 컨테이너 재배포 시 모든 사용자의 paging 상태 사라짐 — 진행 중이던 더보기 세션이 끊김.

### 결함 #2 — Impression dedupe set process-local (REQ-CHAT-STATE-002 의 동기)

같은 파일 line 80-95:

```python
# SPEC-IMPLICIT-FB-001 / REQ-FB-IMPRESSION-001 — per-chat set of product_ids
# already impression-logged this process lifetime. `ai.card_impression` has NO
# unique constraint / ON CONFLICT (migration 0002 — only a NON-unique
# (chat_id, product_id) index), so the same item re-shown via `cards:more`
# (offset paging) or a defensive re-entry would INSERT a duplicate row,
# inflating no-click attribution. Dedupe HERE, at the single delivery seam.
# Memory bound: the inner set is CLEARED at the start of every new search
# (offset==0 fresh-recommendation delivery), so each set stays ≤ the search cap
# (tiny). The outer dict grows by distinct chat_id only — accepted, identical
# structure/precedent to the pre-existing `_CARD_BATCH_CURSOR`; deliberately NO
# LRU/cap (would be over-engineering for the same bound the codebase already
# tolerates).
_LOGGED_IMPRESSION_IDS: dict[int, set[str]] = {}
```

`_log_delivered_impressions` 가 매 batch 발사마다 호출되며, 이 set 으로 "이번 chat 에서 이미 INSERT 한 product_id" 멤버십을 체크해 중복 INSERT 를 차단한다. `ai.card_impression` 테이블에는 UNIQUE 제약 / ON CONFLICT 가 없으므로(migration 0002 가 NON-unique `(chat_id, product_id)` 인덱스만 깐다) **이 set 이 유일한 dedupe 보호망**이다.

문제:

- 결함 #1 과 동일한 (a) 메모리 누수.
- 멀티-워커 시 워커별 set 분리 → 같은 product 가 worker A 에서 INSERT 된 뒤 worker B 가 다시 INSERT → `ai.card_impression` 에 중복 row 누적. Langfuse implicit feedback score 의 분모(impression 수)가 부풀려져 click-through rate 가 잘못 낮게 계산됨. P0 머지된 SPEC-IMPLICIT-FB-001 의 score retro-attach 정확도 손상.
- 재시작 reset 자체는 무해(주석에 명시: "at worst one extra impression row per item after a restart, bounded by the search cap"). 다만 멀티-워커 reset 은 turn 단위로 발생하므로 누적량이 다름.

### 결함 #3 — 다음 분기 스케일 차단

현재 SPEC 들(SPEC-AGENT-V2-CLEANUP-001, SPEC-IMPLICIT-FB-001, SPEC-OBSERVABILITY-002)은 모두 single-process / single-worker 가정. 사용자 증가 시 첫 스케일 옵션이 uvicorn `--workers N` 인데, 위 두 dict 가 그 시점 가장 먼저 깨지는 invariant. **확장 전에** 외부 store 로 옮기는 게 비용이 가장 낮다 — webhook 로직 변경 0, 테스트 표면 좁음 (헬퍼 4개 함수 단위 테스트로 닫힘).

### 왜 한 SPEC 에 묶는가

두 dict 는 다른 invariant(cursor vs dedupe set)를 보호하지만 공통점:

- 같은 파일(`respond.py`)의 같은 컨벤션(module-global dict keyed on chat_id).
- 같은 외부 store(Redis) 로 이전.
- 같은 fail-open 정책.
- 같은 라이프사이클 hook(`app/main.py` lifespan 의 pool warm + close).
- 같은 테스트 라이브러리(`fakeredis`).
- 같은 env (`REDIS_URL` 단일).

별도 SPEC 으로 쪼개면 redis pool 인프라가 두 번 도입 — 어색. 한 묶음이 자연스럽다.

### 왜 dev-ai 기존 redis 컨테이너 DB 1 인가

| 옵션 | 평가 |
|---|---|
| **dev-ai 기존 redis DB 1** (선택) | Langfuse v3 가 이미 깐 redis 컨테이너(`kikoai-ai_app-net` 동일망)의 DB 0 와 분리. 새 인프라 0. 같은 EC2, 같은 docker network — 지연 < 1ms. 비용 0. |
| AWS ElastiCache | dev 단계 비용 부담. 운영 전환 시 별도 고려 (out of scope). |
| 새 redis 컨테이너 | 같은 EC2 에 두 redis 컨테이너 → 메모리 중복. 의미 없음. |
| 그대로 process-local + 워커 1 유지 | 미래 스케일 막힘. dict 사이즈 모니터링 의무 발생. |

dev-ai 기존 redis 컨테이너는 Langfuse v3 self-host 의 일부로 이미 docker-compose 에 정의되어 있고 (`aws-infra/kiko-ai-servers/portal-ai/`), `kikoai-ai_app-net` 안에서 `redis:6379` 호스트명으로 도달 가능. DB 분리(0 → Langfuse, 1 → kiko chat-state)로 키 충돌 없음.

---

## Architecture Snapshot (informative)

Today (pre-SPEC):

```
[respond.py — module-global]
_CARD_BATCH_CURSOR: dict[int, int]            ← process-local, 영구 누적
_LOGGED_IMPRESSION_IDS: dict[int, set[str]]   ← process-local, 영구 누적

[send_hybrid_batch]
  ... album+summary 발사 ...
  _CARD_BATCH_CURSOR[chat_id] = next_offset   ← write
  ↓
[cards:more callback → send_hybrid_batch(offset=None)]
  offset = _CARD_BATCH_CURSOR.get(chat_id, 0) ← read

[_log_delivered_impressions]
  if is_fresh_search:
    _LOGGED_IMPRESSION_IDS.pop(chat_id, None) ← clear
  seen = _LOGGED_IMPRESSION_IDS.setdefault(chat_id, set())
  for c in batch:
    if pid in seen: continue
    seen.add(pid)
    fresh.append(c)
  await log_impressions(chat_id, ..., fresh)
```

After this SPEC:

```
[app/infrastructure/cache/chat_state.py — NEW]
async def get_cursor(chat_id) -> int                 ← Redis GET kiko:cursor:{chat_id}, 없으면 0
async def set_cursor(chat_id, n) -> None             ← Redis SETEX 24h
async def is_logged(chat_id, pid) -> bool            ← Redis SISMEMBER kiko:imp:{chat_id}
async def mark_logged(chat_id, pid) -> None          ← Redis SADD + EXPIRE 7d
async def clear_logged(chat_id) -> None              ← Redis DEL
# 모듈-내 lazy pool(싱글톤). 모든 함수 fail-open(try/except → debug log → 안전 default).

[send_hybrid_batch]
  ... album+summary 발사 ...
  await set_cursor(chat_id, next_offset)             ← redis-backed
  ↓
[cards:more callback → send_hybrid_batch(offset=None)]
  offset = await get_cursor(chat_id)                 ← redis-backed, fail → 0

[_log_delivered_impressions]
  if is_fresh_search:
    await clear_logged(chat_id)                      ← redis-backed
  fresh: list = []
  for c in batch:
    pid = _product_id_of(c)
    if pid is None:
      fresh.append(c); continue                       ← id 없는 candidate 통과(기존 동작 유지)
    if await is_logged(chat_id, pid):
      continue
    await mark_logged(chat_id, pid)
    fresh.append(c)
  await log_impressions(chat_id, ..., fresh)
```

기존 `_CARD_BATCH_CURSOR`, `_LOGGED_IMPRESSION_IDS` 모듈 글로벌 dict 및 `reset_card_batch_cursor_for_tests` 함수 — **완전히 삭제**. 테스트는 `fakeredis` flushdb 또는 헬퍼 `clear_logged` 호출로 교체.

**Affected modules in kikoai/ai** (exact filenames refined in `plan.md`):

- `app/infrastructure/cache/__init__.py` — NEW. (빈 패키지 마커).
- `app/infrastructure/cache/chat_state.py` — NEW. 4개 async 함수(`get_cursor` / `set_cursor` / `is_logged` / `mark_logged` / `clear_logged`) + 모듈-내 lazy redis pool 싱글톤. 모든 함수 fail-open(try/except + `logger.debug` + 안전 default).
- `app/agents/tools/respond.py` — MODIFIED. (1) module-global `_CARD_BATCH_CURSOR` / `_LOGGED_IMPRESSION_IDS` / `reset_card_batch_cursor_for_tests` 삭제. (2) `send_hybrid_batch` 의 cursor read/write 2 지점을 `await get_cursor / set_cursor` 로 교체. (3) `_log_delivered_impressions` 의 `pop` / `setdefault` / `seen` 검사 / `seen.add` 4 지점을 `await clear_logged / is_logged / mark_logged` 로 교체.
- `app/main.py` — MODIFIED. lifespan 에 redis pool warm(startup) + close(shutdown). warm 실패해도 startup 진행(fail-open).
- `app/core/config.py` — MODIFIED. `REDIS_URL: str = "redis://localhost:6379/1"` 신규 필드.
- `pyproject.toml` — MODIFIED. `redis>=5.0` (runtime), `fakeredis>=2.0` (dev dependency).
- `docker-compose.yml` — MODIFIED (로컬 개발용). `redis:7-alpine` 서비스 신규 + `ai-server` 의 `depends_on: redis` + `REDIS_URL=redis://redis:6379/1` env.
- `tests/test_infrastructure/__init__.py` — NEW.
- `tests/test_infrastructure/cache/__init__.py` — NEW.
- `tests/test_infrastructure/cache/test_chat_state.py` — NEW. fakeredis 기반 ~12 케이스(happy path × 5 함수, connection error fail-open × 5 함수, TTL 검증 × 2).
- `tests/test_agents/tools/test_respond_redis_integration.py` — NEW(또는 기존 respond 테스트에 추가). fakeredis fixture 위에서 `send_hybrid_batch` / `_log_delivered_impressions` 통합 검증.

**Reused, untouched modules**:

- `app/channels/implicit_feedback.py` (`log_impressions`, `_product_id_of`) — 무변경. dedupe 가 caller 측(`respond.py`)에 있다는 invariant 유지.
- `app/channels/pre_messages.py::_NODE_MARKERS` — 이미 cap 64 + half-eviction LRU. 본 SPEC 범위 NOT.
- `app/infrastructure/memory/session.py` / `session_pg.py` / `taste_profile.py` / `taste_profile_pg.py` — 무변경. session/taste 저장은 PostgreSQL 유지(별도 SPEC).
- 다른 ReAct tool(`search_products`, `refine_search`, `update_taste`, `ask_user_clarification`, `analyze_image`, `get_recent_history`, `suggest_next_step`) — 무관.
- Langfuse self-host(redis DB 0) — 무관(DB 1 사용).

---

## Requirements & Acceptance Criteria

### REQ Index

| REQ-ID | Title | Priority |
|---|---|---|
| REQ-CHAT-STATE-001 | "더보기" 페이저 cursor 를 Redis 키 `kiko:cursor:{chat_id}` (TTL 24h) 로 외부화 | P1 |
| REQ-CHAT-STATE-002 | Impression dedupe set 을 Redis 키 `kiko:imp:{chat_id}` (TTL 7d, 새 검색 시 DEL) 로 외부화 | P1 |
| REQ-CHAT-STATE-003 | 모든 Redis 호출 fail-open — 추천 카드 발사는 절대 차단/지연/raise 하지 않음 | P1 |
| REQ-CHAT-STATE-004 | 로컬(docker-compose redis) / 테스트(fakeredis) / prod(dev-ai 기존 redis DB 1) 3환경을 단일 `REDIS_URL` 환경변수로 분기 | P1 |

---

### Pager Cursor → Redis (REQ-CHAT-STATE-001)

#### REQ-CHAT-STATE-001 — "더보기" 페이저 cursor 를 Redis 키 `kiko:cursor:{chat_id}` (TTL 24h) 로 외부화 [P1]

**WHEN** `send_hybrid_batch` 가 album+summary 5장 발사를 완료할 때,
**THE SYSTEM SHALL** 다음 페이지 시작 인덱스(`next_offset: int`)를 Redis 키 `kiko:cursor:{chat_id}` 에 TTL 24시간(86400초) 으로 저장한다. (`SETEX` 또는 `SET ... EX 86400` 동치 명령 사용 — 정확한 명령은 plan.md 가 결정.)

**WHEN** `cards:more` callback 으로 `send_hybrid_batch(offset=None)` 가 호출될 때,
**THE SYSTEM SHALL** Redis 키 `kiko:cursor:{chat_id}` 를 GET 해 정수로 파싱한 값을 다음 batch 시작 인덱스로 사용한다. 키가 없거나 Redis 호출이 실패하면 `0` 으로 fallback(첫 페이지부터 다시 — 기존 dict 미스 시 동작과 동일).

**THE SYSTEM SHALL** 기존 모듈 글로벌 `_CARD_BATCH_CURSOR: dict[int, int]` 변수를 `app/agents/tools/respond.py` 에서 **완전히 제거**한다. `reset_card_batch_cursor_for_tests` 함수도 함께 제거하거나 새 헬퍼(`clear_logged` 또는 `flushdb`)에 위임한다.

**Rationale**: process-local dict 는 멀티-워커 도입 시 워커별 cursor 가 분리되어 "더보기" 탭에서 같은 페이지 재노출이 발생한다. Redis 단일 source 는 chat_id 별 cursor 일관성을 워커 수와 무관하게 보장한다. TTL 24h 는 사용자가 하루 안에 paging 재개할 가능성을 커버하고, 그 이후엔 자연 만료로 메모리 회수.

**Acceptance**:

- 단위 테스트(`fakeredis`): `set_cursor(chat_id=42, n=5)` 호출 후 `get_cursor(42)` 가 `5` 반환. 키 만료 검증(`fakeredis` 의 `TTL` 명령 또는 시간 advance).
- 단위 테스트: `get_cursor(42)` 가 unset 키에서 `0` 반환.
- 단위 테스트: `set_cursor` 후 fakeredis 클라이언트의 `ttl("kiko:cursor:42")` 가 86400 (또는 그 이하 양수) 반환.
- 통합 테스트(`fakeredis` fixture): `send_hybrid_batch(state, offset=0, ...)` 호출 후 `kiko:cursor:{chat_id}` 가 정확히 다음 페이지 시작 인덱스(예: 5)로 set. 후속 `send_hybrid_batch(state, offset=None, ...)` 호출이 그 값을 읽어 5번째 인덱스부터 batch 발사.
- AST/grep 회귀: `app/agents/tools/respond.py` 에서 `_CARD_BATCH_CURSOR` literal 이 완전히 사라졌음. `grep -n "_CARD_BATCH_CURSOR" app/` 결과 빈 set.
- 기존 cursor 관련 테스트가 새 헬퍼로 마이그레이션되어 모두 green.

---

### Impression Dedupe Set → Redis (REQ-CHAT-STATE-002)

#### REQ-CHAT-STATE-002 — Impression dedupe set 을 Redis 키 `kiko:imp:{chat_id}` (TTL 7d, 새 검색 시 DEL) 로 외부화 [P1]

**WHEN** `_log_delivered_impressions` 가 호출될 때,
**THE SYSTEM SHALL** Redis 키 `kiko:imp:{chat_id}` (SET 타입) 에서 batch 내 각 candidate 의 `product_id` 멤버십(`SISMEMBER`)을 확인하고, 아직 등록되지 않은 product_id 만 `ai.card_impression` INSERT 대상 리스트(`fresh`)에 추가한 뒤 set 에 `SADD` 한다. 매 `SADD` 이후 또는 매 새 키 생성 시 `EXPIRE` 7일(604800초)을 설정한다(정확한 명령 순서는 plan.md 가 결정).

**WHEN** `is_fresh_search` 가 True 일 때 (offset==0 — 새 검색의 첫 batch),
**THE SYSTEM SHALL** `kiko:imp:{chat_id}` 키를 `DEL` 후 새 dedupe set 을 시작한다. 기존 의미 보존: 새 검색은 prior search 의 dedupe 를 무시하고, 같은 product 라도 새 trace 에 다시 attribute 되어야 한다(SPEC-IMPLICIT-FB-001 의 score retro-attach 정합성).

**WHERE** candidate 의 `product_id` 가 `None` 일 때,
**THE SYSTEM SHALL** dedupe 우회 — `fresh` 리스트에 그대로 append(기존 `log_impressions` 가 id-less candidate 를 자체 skip 하는 동작 보존).

**THE SYSTEM SHALL** 기존 모듈 글로벌 `_LOGGED_IMPRESSION_IDS: dict[int, set[str]]` 변수를 `app/agents/tools/respond.py` 에서 **완전히 제거**한다.

**Rationale**: `ai.card_impression` 테이블에는 UNIQUE 제약 / ON CONFLICT 가 없으므로 이 dedupe 가 유일한 중복 INSERT 보호망이다. 멀티-워커 시 워커별 dict 분리는 곧 dedupe 우회 → 중복 row 누적 → Langfuse implicit feedback score 의 impression 분모 부풀림 → click-through rate 잘못 낮게 계산. Redis 단일 source 는 chat_id 별 dedupe 일관성을 워커 수와 무관하게 보장한다. TTL 7d 는 cards:more paging 의 합리적 최대 기간 + 안전 마진.

**Acceptance**:

- 단위 테스트(`fakeredis`): `is_logged(42, "prod-A")` 가 False(unset 키), `mark_logged(42, "prod-A")` 후 `is_logged(42, "prod-A")` True, `is_logged(42, "prod-B")` False.
- 단위 테스트: `mark_logged(42, "prod-A")` 후 fakeredis `ttl("kiko:imp:42")` 가 604800 (또는 그 이하 양수).
- 단위 테스트: `mark_logged(42, "prod-A")` → `clear_logged(42)` → `is_logged(42, "prod-A")` False.
- 통합 테스트: 5개 candidate 발사 → `kiko:imp:{chat_id}` 에 5개 product_id 추가됨. 같은 5개 재발사 (cards:more, `is_fresh_search=False`) → `log_impressions` INSERT 0건 호출 + set 멤버 그대로 5개.
- 통합 테스트: 5개 발사 후 새 검색(`is_fresh_search=True`)으로 동일 5개 발사 → `kiko:imp:{chat_id}` 가 DEL 후 새로 5개 등록 + `log_impressions` 5건 모두 INSERT.
- 통합 테스트: candidate 중 `product_id=None` 인 1개 + 정상 4개 → fresh 리스트 길이 5, 정상 4개만 set 멤버.
- AST/grep 회귀: `app/agents/tools/respond.py` 에서 `_LOGGED_IMPRESSION_IDS` literal 이 완전히 사라졌음. `grep -n "_LOGGED_IMPRESSION_IDS" app/` 결과 빈 set.

---

### Fail-Open Policy (REQ-CHAT-STATE-003)

#### REQ-CHAT-STATE-003 — 모든 Redis 호출 fail-open — 추천 카드 발사는 절대 차단/지연/raise 하지 않음 [P1]

**WHEN** Redis 연결 또는 명령이 실패할 때 (네트워크, timeout, AUTH 실패, redis OOM, redis 다운 등 어떤 예외든),
**THE SYSTEM SHALL** 다음 모두를 만족한다:

1. **추천 카드 발사는 계속 진행**:
   - `get_cursor` 실패 → `0` 반환 (첫 페이지부터 다시 — 기존 dict 미스 시 동작 byte-identical).
   - `set_cursor` 실패 → 무영향(다음 호출 시 cursor 못 읽으면 `0` fallback — paging 1회 끊김 수용).
   - `is_logged` 실패 → `False` 반환(중복 INSERT 허용 — 기존 dict 가 없을 때와 동일 행동, `ai.card_impression` 에 중복 row 가 생기지만 functional 동작 무결).
   - `mark_logged` 실패 → 무영향(다음 호출 시 `is_logged` 가 False → 중복 INSERT 가능 — 위와 동일).
   - `clear_logged` 실패 → 무영향(다음 호출 시 잔여 set 멤버가 신규 검색에 끼어듦 — 1회분 dedupe noise 수용).
2. **`logger.debug` 로 1줄 기록** (warning 아님 — Redis 일시 장애가 일반적이라 warn 은 too noisy. 운영에서 지속 실패 모니터링은 redis 자체 메트릭으로 별도).
3. **절대 raise 하지 않음** — 모든 redis 호출은 try/except 로 wrap. caller (`respond.py`) 는 헬퍼만 await 하고 별도 try/except 가드를 추가하지 않는다(헬퍼 안에서 swallow).

**THE SYSTEM SHALL** redis pool 자체의 초기화 실패(예: `REDIS_URL` 미설정, host unreachable) 도 process startup 을 막지 않는다 — `app/main.py` lifespan 의 redis warm 은 fail-open으로 처리되고, 이후 헬퍼 호출 시 lazy 재시도(또는 매 호출 fail-open `False`/`0`/no-op 반환).

**Rationale**: 본 SPEC 의 동기는 메모리 누수 차단 + 멀티-워커 일관성 확보. 추천 시스템 자체의 가용성을 redis 가용성에 종속시키면 안 된다(redis 한 줄 장애가 추천 전체 차단으로 번지면 본말전도). 기존 process-local dict 는 본질적으로 fail-open(읽기 미스 시 default) — Redis 도 같은 의미적 보장.

**Acceptance**:

- 단위 테스트: redis client mock 이 모든 명령에 대해 `redis.ConnectionError` raise → `get_cursor` 가 `0` 반환(raise 없음), `set_cursor` no-op 반환(raise 없음), `is_logged` False 반환, `mark_logged` no-op, `clear_logged` no-op.
- 단위 테스트: redis client mock 이 `asyncio.TimeoutError` raise → 5개 함수 모두 동일하게 fail-open.
- 단위 테스트: 위 실패 케이스 각각에서 caplog 에 정확히 1줄의 DEBUG 라인(`logger.debug`) 기록. WARN/ERROR 없음.
- 단위 테스트: `app/main.py` lifespan startup 시 redis 연결 불가(잘못된 `REDIS_URL`) → process startup 은 정상 진행(테스트 환경에서 FastAPI TestClient 가 정상 startup 완료).
- 통합 테스트: redis 다운 시뮬레이션(fakeredis 의 `connection_error` 모드) 에서 `send_hybrid_batch` 호출 → 카드 발사 정상 완료 + `log_impressions` 정상 호출 + cursor read=0 처리.

---

### 3-Environment Support (REQ-CHAT-STATE-004)

#### REQ-CHAT-STATE-004 — 로컬 / 테스트 / prod 3환경을 단일 `REDIS_URL` 환경변수로 분기 [P1]

**WHEN** 개발자가 로컬에서 봇 수동 테스트를 위해 `docker compose up -d` 를 실행할 때,
**THE SYSTEM SHALL** 로컬 `docker-compose.yml` 에 새로 추가된 `redis:7-alpine`(또는 동치) 서비스가 함께 기동되고, `ai-server` 서비스가 `depends_on: redis` + 환경변수 `REDIS_URL=redis://redis:6379/1` 로 자동 연결된다.

**WHEN** `pytest` 가 실행될 때,
**THE SYSTEM SHALL** dev dependency `fakeredis>=2.0` 가 real redis 인스턴스 없이 redis 명령을 in-process 로 시뮬레이션한다. 테스트 fixture(`@pytest.fixture` 또는 `conftest.py`)가 매 테스트 케이스마다 fakeredis 인스턴스를 새로 만들어 헬퍼의 redis pool 싱글톤에 주입한다.

**WHEN** dev-ai EC2 (`i-05e8dbdb3e00ace23`, `54.116.116.225`) 에서 운영될 때,
**THE SYSTEM SHALL** 기존 redis 컨테이너(Langfuse v3 self-host 의 일부, `kikoai-ai_app-net` 동일망)의 **DB 1** (DB 0 는 Langfuse 가 사용) 에 `REDIS_AUTH` 인증으로 연결한다. `REDIS_URL=redis://:${REDIS_AUTH}@redis:6379/1` 형태.

`REDIS_URL` 단일 환경변수가 3환경 모두를 제어한다. 새 환경변수(`REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` 별도 분리 등)는 도입하지 않는다.

**Rationale**: 단일 게이트는 (a) 환경별 설정 오류 표면을 최소화하고, (b) python `redis` 라이브러리의 표준 URL 파싱(scheme, auth, db number 포함)을 그대로 활용하며, (c) Langfuse 가 이미 같은 방식으로 `REDIS_URL` 을 쓰고 있어 일관성. flag 없음 — SPEC-AGENT-V2-CLEANUP-001 "no feature flags" 정책 일관.

**Acceptance**:

- 로컬 검증: `docker compose up -d` 후 `docker ps` 에 `redis` 컨테이너 running, `ai-server` 컨테이너 startup log 에 "redis pool warmed (db=1)" 또는 동치 메시지(또는 fail-open 시 "redis warm skipped: <reason>" debug log).
- 로컬 검증: `docker exec` 로 redis CLI 진입 → `SELECT 1` → `KEYS kiko:*` 가 사용자 활동 후 채워짐. DB 0 (`SELECT 0`) 에는 Langfuse 키만 존재 (`KEYS kiko:*` 빈 set).
- 테스트 검증: `pytest tests/test_infrastructure/cache/test_chat_state.py` 가 real redis 컨테이너 없이 fakeredis 위에서 모두 green. CI 환경(GitHub Actions 등)에서도 redis service 불필요.
- prod 검증: dev-ai 배포 후 `docker exec ai-server env | grep REDIS_URL` 가 `redis://:****@redis:6379/1` 형식 확인. `docker exec redis redis-cli -a ${REDIS_AUTH} -n 1 KEYS 'kiko:*'` 가 첫 사용자 활동 후 키 반환.
- 환경변수 누락 시: `REDIS_URL` 미설정 → `app/core/config.py` 의 default `"redis://localhost:6379/1"` 사용 → 로컬 redis 없으면 헬퍼들이 fail-open 으로 안전 default 반환 (process startup 정상).

---

## Exclusions (What NOT to Build)

The following are explicitly NOT delivered by SPEC-CHAT-STATE-REDIS-001 and MUST NOT be conflated with it:

1. **`app/channels/pre_messages.py::_NODE_MARKERS` 의 Redis 이전.** 이 dict 는 이미 cap 64 + half-eviction LRU 처리되어 메모리 누수 위험이 closed. 본 SPEC 범위 NOT.
2. **`session_pg.py` / `taste_profile_pg.py` 의 PostgreSQL → Redis 마이그레이션.** 더 큰 작업(타입/스키마/조회 패턴 변경). 별도 SPEC 으로 분리.
3. **Redis 클러스터링, sentinel, 백업 정책.** dev 단계 self-host single-node 유지. prod 전환 시 별도 SPEC.
4. **AWS ElastiCache 전환.** dev 단계 self-host 유지(비용). 운영 트래픽 도달 시 별도 SPEC.
5. **Apify rate-limit, LiteLLM, Modal 등 다른 외부 의존성의 redis 화.** 본 SPEC 범위 NOT.
6. **세션/유저별 영구 cursor history 또는 paging analytics.** dedupe set 과 cursor 는 일시 운영 상태일 뿐 분석 데이터가 아님. analytics 는 `ai.card_impression` + Langfuse trace 가 source of truth.
7. **TTL 24h / 7d 의 사용자별 조정 또는 동적 변경.** 고정값. 변경 시 SPEC version bump.
8. **Cursor / dedupe set 의 backfill 또는 마이그레이션.** SPEC 머지 시점부터 적용 — 기존 process-local dict 의 내용은 process 재시작과 함께 사라지고 새 키가 redis 에 깔린다. 사용자 관점 1회성 paging 끊김 수용(이미 컨테이너 재시작 시 동일하게 발생하던 경험).
9. **새 env var(`REDIS_AUTH` 외) 도입.** `REDIS_URL` 단일 게이트. 보안/host/port/db 모두 URL 안에 포함.
10. **Redis 메트릭 모니터링 / Grafana 보드.** 별도 SPEC. 본 SPEC 의 fail-open 정책 자체가 redis 가용성에 추천 가용성을 종속시키지 않으므로 즉시 알림 의무 낮음.
11. **Cursor 값의 정확성을 보장하기 위한 race-condition 가드.** `SETEX` 는 idempotent(같은 chat_id 의 후속 `set_cursor` 가 prior 값을 덮어쓰는 게 정상 의미). MULTI/EXEC 트랜잭션 또는 Lua 스크립트 도입 NOT.
12. **`reset_card_batch_cursor_for_tests` 와 동치인 prod-side API.** 테스트 hook 만 새 헬퍼로 마이그레이션. prod 에서 cursor 강제 reset 이 필요하면 redis CLI 로 직접 `DEL` (out of scope but trivial).
13. **kikoai/app (Next.js) 또는 다른 캡 caller 의 redis 사용.** 본 SPEC 의 chat-state 는 ai server 전용. kikoai/app 은 별개.
14. **다른 chat-state 후보 발굴.** 본 SPEC 은 명시된 2개 dict 만 다룬다. 새 chat-state 가 누적되면 별도 SPEC.

---

## Stakeholders

| Role | Responsibility |
|---|---|
| Product / Founder (hchsa77@gmail.com) | 메모리 누수 + 멀티-워커 스케일 차단 동기 확인. fail-open 정책(redis 다운 시 추천 자체는 정상 작동) 컨펌. dev-ai 기존 redis DB 1 사용 컨펌. TTL 24h/7d 컨펌. 로컬 docker-compose + 테스트 fakeredis + prod dev-ai redis 3환경 분기 컨펌. |
| AI Server Owner (this SPEC) | `app/infrastructure/cache/chat_state.py` (NEW), `app/agents/tools/respond.py` (MODIFIED — 두 dict + reset 함수 제거 + 4 지점 헬퍼 교체), `app/main.py` (MODIFIED — lifespan redis pool warm/close), `app/core/config.py` (MODIFIED — `REDIS_URL` 필드), `pyproject.toml` (MODIFIED — redis + fakeredis), `docker-compose.yml` (MODIFIED — redis 서비스). 신규 테스트 2 파일. |
| dev-ai EC2 운영 | dev-ai 의 기존 redis 컨테이너에 `REDIS_AUTH` 가 설정되어 있는지 확인(Langfuse v3 self-host 가 이미 설정). DB 1 사용에 대한 컨테이너 환경변수(`REDIS_URL=redis://:${REDIS_AUTH}@redis:6379/1`) 를 `/home/ec2-user/.env` 또는 동치 위치에 추가. aws-infra 리포의 docker-compose 추적(없으면 재배포 시 회귀 — memory: `dev-ai Telegram setup` 패턴). |
| kikoai/app, Modal, Langfuse | Out of scope. Langfuse 는 같은 redis 컨테이너의 DB 0 를 그대로 사용 — 본 SPEC 의 DB 1 사용은 무영향. |

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Redis 다운 시 추천 차단.** redis 일시 장애가 사용자 추천 전체를 막으면 본말전도. | Low (fail-open 정책) | High (만약 발생 시) | REQ-CHAT-STATE-003 의 모든 fail-open 절은 단위 테스트로 강제. caller(`respond.py`) 는 try/except 가드 없이 헬퍼만 await — 헬퍼 안에서 swallow. AST/grep 회귀로 caller 측 raise propagation 차단. |
| R2 | **Langfuse 와 키 충돌.** 같은 redis 컨테이너의 DB 0 (Langfuse) 와 DB 1 (kiko chat-state) 가 분리되었으나 운영 실수로 같은 DB 가 쓰일 경우 키 prefix 충돌 우려. | Low | Medium | (a) `REDIS_URL` 의 `/1` 명시. (b) 모든 키에 `kiko:` prefix(`kiko:cursor:{chat_id}`, `kiko:imp:{chat_id}`) — Langfuse 키 패턴(`bull:...`, `langfuse:...`)과 겹치지 않음. (c) 런타임 검증: lifespan startup 시 `CLIENT INFO` 또는 `CLIENT GETNAME` 으로 db 번호 로그. |
| R3 | **Redis 메모리 압박.** chat_id × product_id 의 누적 set 이 메모리 압박. | Low (TTL 7d + 새 검색 시 DEL) | Low | TTL 7d 자연 만료. 새 검색마다 DEL — 활성 사용자 chat 당 set 사이즈 ≤ 검색 cap × paging 횟수 (수십~수백 멤버). 1만 chat 활성 시 ~10MB. dev-ai redis(Langfuse 와 공유)의 maxmemory 정책으로 보호. |
| R4 | **단위 테스트의 fakeredis 행동이 real redis 와 다르다.** fakeredis 의 TTL/expire 시뮬레이션이 real redis 와 다른 edge case 가 있을 수 있다. | Medium | Low | (a) 정수 카운터/SET 연산은 fakeredis 가 fully 지원(verified 라이브러리). (b) TTL 검증은 "양수 반환" 만 assert (정확한 초 단위 비교 회피). (c) 통합 smoke 는 dev-ai 실 환경에서 manual 검증. |
| R5 | **`REDIS_URL` 미설정 시 default `redis://localhost:6379/1` 가 prod 에서 잘못 동작.** prod EC2 에 localhost redis 가 없으면 즉시 fail-open 으로 통과 — 실수가 silent. | Medium | Medium | (a) lifespan startup 의 warm 결과(`PING` 응답 OK/FAIL)를 로그에 명시. (b) `/home/ec2-user/.env` 의 `REDIS_URL` 누락은 dev-ai 운영 checklist 에 추가. (c) `aws-infra` docker-compose 의 `ai-server` env block 에 `REDIS_URL` 명시(코드 추적). |
| R6 | **`reset_card_batch_cursor_for_tests` 제거로 기존 외부 테스트 회귀.** 다른 테스트 파일에서 이 함수를 import 한다면 import error. | Medium | Low | (a) grep `reset_card_batch_cursor_for_tests` 로 caller 검색 후 모두 새 패턴(fakeredis fixture 또는 `clear_logged`)으로 마이그레이션. (b) plan.md UX-T01 의 inspection 단계에 포함. |
| R7 | **redis pool 싱글톤의 첫 호출 race.** lazy 초기화 시 동시 다중 호출이 같은 client 두 개 생성 시도. | Low | Low | python `redis.asyncio.Redis.from_url` 는 thread-safe(asyncio loop 단일 스레드 가정). 모듈-내 module-level `_pool` 변수 + 첫 호출 시 `_pool or create()` 패턴이면 충분(GIL + 단일 event loop). 별도 lock 불필요. |
| R8 | **TTL 24h vs 사용자 paging 세션 길이.** 사용자가 24시간 이상 지난 후 "더보기" 탭 시 cursor 만료 → 처음부터 다시 노출. UX 끊김. | Low | Low | 일반 사용자는 한 검색 안에서 paging 을 빠르게 마침(분~시간 단위). 24h 는 안전 마진. 만약 사용자 행동 데이터로 부족 입증 시 SPEC version bump 으로 36h/48h 로 조정. |
| R9 | **fakeredis 라이브러리 maintenance.** dev dep 추가가 향후 broken 가능성. | Low | Low | `fakeredis>=2.0` 은 active project(2026 기준 정기 릴리스). pin 정책은 `pyproject.toml` lockfile 로 충분. |
| R10 | **id-less candidate 의 dedupe 우회 회귀.** SPEC 은 product_id=None 시 fresh 리스트 append 만 — 기존 동작 보존. 만약 구현이 잘못해 `None` 을 redis key 로 만들면 키 충돌. | Low | Medium | helper `is_logged` / `mark_logged` 의 signature 가 `pid: str` 명시 + caller 측 `if pid is None: append; continue` 가드 + 단위 테스트(`id=None` 케이스). plan.md 가 헬퍼 시그니처 lock. |
| R11 | **운영 redis container 의 maxmemory 정책 충돌.** Langfuse 가 같은 redis 를 ClickHouse queue 로 활발히 사용 중 — kiko 키가 큰 비중 차지 시 eviction 정책으로 인해 cursor/dedupe 가 silently 사라질 수 있다. | Medium | Low | (a) kiko 키 사이즈는 R3 기준 작음(~10MB). (b) Redis maxmemory-policy 가 `allkeys-lru` 이면 LRU 로 제거 — fail-open 정책으로 추천에 무영향. (c) 운영 모니터링: dev-ai redis `INFO memory` 주기 체크 — 별도 SPEC. |

---

## Open Questions (deferred to plan.md / implementation)

본 SPEC 단계에서 의도적으로 deferred. 본 SPEC 승인을 막지 않지만 코드 작성 전 plan.md 에서 결정해야 한다:

1. **redis pool 의 lazy 초기화 패턴.** 모듈-내 module-level `_pool: redis.asyncio.Redis | None = None` + 첫 호출 시 `_pool = redis.from_url(REDIS_URL)` vs lifespan 에서 사전 주입. plan.md 가 결정 — 권장: lifespan 에서 warm 시도(실패 시 None) + 헬퍼는 lazy fallback.
2. **`set_cursor` 의 정확한 redis 명령.** `SETEX kiko:cursor:{chat_id} 86400 <value>` vs `SET kiko:cursor:{chat_id} <value> EX 86400` 동치. plan.md 가 선택.
3. **`mark_logged` 의 EXPIRE 호출 빈도.** 매 `SADD` 마다 `EXPIRE` 호출 vs 첫 `SADD` 만 (`SET ... NX` 패턴) — Redis 7+ 에서 `EXPIRE ... NX` 옵션 사용 권장. plan.md 가 결정.
4. **`is_fresh_search` 의 정확한 판정 위치.** 기존 `_log_delivered_impressions` 의 caller 시그니처 그대로 `is_fresh_search: bool` 파라미터 사용. plan.md 가 caller 측에서 어떤 조건으로 True 를 set 하는지 명시(주석 보강).
5. **fakeredis fixture 의 scope.** 매 테스트마다 새 fakeredis 인스턴스(function scope) vs session scope + 매 케이스 flushdb. 권장: function scope (단순). plan.md lock.
6. **헬퍼 함수의 `chat_id` 타입.** `int` vs `str`. 기존 코드는 `int(chat_id)` 변환 패턴 (`_LOGGED_IMPRESSION_IDS.pop(int(chat_id), None)`). 헬퍼는 `chat_id: int` 만 받고 내부에서 str 변환 권장. plan.md 가 lock.
7. **lifespan 의 redis warm 실패 로그 레벨.** 권장: `logger.info` (정보성 — 의도된 fail-open 임을 명시). `warning` 도 가능하지만 noisy. plan.md lock.
8. **`REDIS_URL` default 값.** `"redis://localhost:6379/1"` 권장(로컬 docker-compose 와 일치). plan.md lock.
9. **`docker-compose.yml` 의 redis 이미지 버전.** `redis:7-alpine` (Langfuse 와 호환) vs `redis:7.4-alpine`. plan.md 가 dev-ai prod 의 정확한 버전과 일치하도록 lock.
10. **CLAUDE.md 업데이트 범위.** "핵심 파일" 표에 `app/infrastructure/cache/chat_state.py` 신규 행 + `respond.py` 설명에서 module-global dict 언급 제거. `docs/features/observability.md` 또는 architecture 문서가 impression dedupe 메커니즘 언급하면 redis 기반으로 갱신. plan.md 가 정확한 docs 갱신 set 을 lock.

---

## Cross-References

- **Builds on (HARD)**:
  - SPEC-IMPLICIT-FB-001 — `_LOGGED_IMPRESSION_IDS` 가 보호하는 invariant(중복 INSERT 차단으로 score 분모 정확성 유지) 가 본 SPEC 의 동기. dedupe 동작 의미는 byte-identical 로 보존되어야 함.
  - SPEC-AGENT-V2-CLEANUP-001 — "no feature flags / single permanent topology" 정책. 본 SPEC 의 두 변경 모두 unconditional, 플래그 없음. `REDIS_URL` 은 환경 설정이지 feature flag 가 아님.
- **Builds on (SOFT)**:
  - SPEC-OBSERVABILITY-002 — Langfuse v3 self-host 가 같은 redis 컨테이너의 DB 0 사용. 본 SPEC 의 DB 1 사용은 docker-compose 의 동일 redis 서비스를 공유.
  - SPEC-CONVERSATION-LOG-001 — `card_sent` / `card_clicked` 이벤트 카운트가 `ai.card_impression` 의 정확성에 의존. 본 SPEC 의 dedupe Redis 화로 멀티-워커 시에도 카운트 정합성 유지.
  - SPEC-AGENT-UX-P0-001 (REQ-UX-001) — diversify 단계 product_id dedup. 본 SPEC 은 impression 단계 dedup(다른 layer) — 충돌 없음, 직교.
- **Triggers / unblocks**:
  - 미래 SPEC: uvicorn `--workers N` 멀티-워커 도입 — 본 SPEC 머지가 prerequisite.
  - 미래 SPEC: ai-server 멀티-인스턴스(blue/green, canary, autoscale) — 본 SPEC 머지가 prerequisite.
  - 미래 SPEC: session_pg / taste_profile_pg 의 Redis 마이그레이션(검토 단계) — 본 SPEC 의 redis pool/lifespan 패턴 재사용 가능.
- **Affected modules in kikoai/ai**:
  - MODIFIED: `app/agents/tools/respond.py` (두 dict + reset 함수 제거, 4 지점 헬퍼 교체), `app/main.py` (lifespan redis warm/close), `app/core/config.py` (`REDIS_URL` 필드), `pyproject.toml` (redis + fakeredis), `docker-compose.yml` (redis 서비스).
  - NEW: `app/infrastructure/cache/__init__.py`, `app/infrastructure/cache/chat_state.py` (4-5 async 헬퍼 + lazy pool).
  - NEW (tests): `tests/test_infrastructure/__init__.py`, `tests/test_infrastructure/cache/__init__.py`, `tests/test_infrastructure/cache/test_chat_state.py` (~12 케이스), `tests/test_agents/tools/test_respond_redis_integration.py` (통합 ~6 케이스).
  - UNCHANGED (asserted): `app/channels/implicit_feedback.py` (`log_impressions`, `_product_id_of`), `app/channels/pre_messages.py::_NODE_MARKERS`, `app/infrastructure/memory/{session,session_pg,taste_profile,taste_profile_pg}.py`, 다른 ReAct tool 들, Langfuse 통합(redis DB 0 무관).
- **Project context**: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md` — 업데이트: (1) "핵심 파일" 표에 `app/infrastructure/cache/chat_state.py` 신규 행 추가. (2) `app/agents/tools/respond.py` 설명에서 module-global dict 언급(_CARD_BATCH_CURSOR, _LOGGED_IMPRESSION_IDS) 제거 및 redis 기반 chat-state 언급 추가. (3) "환경 변수" 절에 `REDIS_URL` 추가.
- **aws-infra**: dev-ai 의 docker-compose.yml(`aws-infra/kiko-ai-servers/portal-ai/`) 에 `ai-server` 서비스의 환경변수 `REDIS_URL=redis://:${REDIS_AUTH}@redis:6379/1` 명시 추가. memory lesson "dev-ai Telegram setup" 패턴 — tracked infra .env 가 키를 보유해야 재배포 시 회귀 방지.
- **Research basis**: `/Users/hansangho/Desktop/kikoai/ai/app/agents/tools/respond.py` line 60-163 직접 인스펙션 (2026-05-20). `_CARD_BATCH_CURSOR` (line 77), `_LOGGED_IMPRESSION_IDS` (line 95), `reset_card_batch_cursor_for_tests` (line 98-101), `_log_delivered_impressions` (line 110-163). `_NODE_MARKERS` cap 64 / half-eviction 동작은 `app/channels/pre_messages.py:69` 별도 인스펙션으로 검증 — 본 SPEC 범위 NOT.

---

## Definition of Done (P1)

- [ ] REQ-CHAT-STATE-001 implemented. `app/infrastructure/cache/chat_state.py` 의 `get_cursor` / `set_cursor` 헬퍼가 Redis 키 `kiko:cursor:{chat_id}` (TTL 24h) 를 read/write. `send_hybrid_batch` 의 cursor 처리 2 지점이 헬퍼로 교체됨. 모듈 글로벌 `_CARD_BATCH_CURSOR` 가 `respond.py` 에서 완전히 제거됨 (grep 검증).
- [ ] REQ-CHAT-STATE-002 implemented. `is_logged` / `mark_logged` / `clear_logged` 헬퍼가 Redis 키 `kiko:imp:{chat_id}` (SET, TTL 7d, 새 검색 시 DEL) 를 관리. `_log_delivered_impressions` 의 4 지점(`pop` / `setdefault` / 멤버십 / `add`)이 헬퍼로 교체됨. 모듈 글로벌 `_LOGGED_IMPRESSION_IDS` 가 `respond.py` 에서 완전히 제거됨 (grep 검증).
- [ ] REQ-CHAT-STATE-003 implemented. 5개 헬퍼 모두 try/except + `logger.debug` 1줄 + 안전 default 반환. caller(`respond.py`) 는 try/except 가드 없이 헬퍼만 await. AST/grep 회귀로 헬퍼 안에서 raise 가 propagate 되지 않음을 확인. `app/main.py` lifespan 의 redis warm 실패가 startup 을 막지 않음.
- [ ] REQ-CHAT-STATE-004 implemented. `app/core/config.py` 의 `REDIS_URL: str` default `"redis://localhost:6379/1"`. `pyproject.toml` 에 `redis>=5.0` (runtime) + `fakeredis>=2.0` (dev). 로컬 `docker-compose.yml` 에 `redis:7-alpine` 서비스 + `ai-server` 의 `depends_on: redis` + `REDIS_URL` env. prod aws-infra docker-compose 에 `REDIS_URL=redis://:${REDIS_AUTH}@redis:6379/1` 명시.
- [ ] 모든 새 테스트 파일 green:
  - `tests/test_infrastructure/cache/test_chat_state.py` (~12 케이스 — happy path × 5 함수 + fail-open × 5 함수 + TTL 검증 × 2).
  - `tests/test_agents/tools/test_respond_redis_integration.py` (~6 케이스 — fakeredis fixture 위 통합 검증).
- [ ] AST/grep 회귀: `grep -nE "_CARD_BATCH_CURSOR|_LOGGED_IMPRESSION_IDS|reset_card_batch_cursor_for_tests" app/` 결과 빈 set (테스트 헬퍼 마이그레이션 완료).
- [ ] 기존 모든 테스트(`uv run pytest -q` baseline) green (no regression).
- [ ] `uv run ruff check . && uv run ruff format --check .` 통과.
- [ ] 로컬 manual smoke: `docker compose up -d` → `docker ps` 에 redis running → 봇에 사진 1장 전송 → 추천 카드 수신 → "더보기" 탭 → 다음 5장 수신(첫 5장과 다른 product_id) → `docker exec redis redis-cli -n 1 KEYS 'kiko:*'` 가 cursor + imp 키 반환.
- [ ] dev-ai prod smoke: 배포 후 1 사용자 시나리오 — 사진 → 카드 → 더보기 → `ssh ... 'docker exec redis redis-cli -a $REDIS_AUTH -n 1 KEYS kiko:*'` 가 키 반환 + `ai.card_impression` 테이블에서 같은 product_id 중복 row 없음 확인 (멀티-워커 미도입 단계라 단일 worker 동작 검증만).
- [ ] CLAUDE.md 핵심 파일 표 업데이트 (3 지점: NEW `chat_state.py` 행 추가 / `respond.py` 설명에서 module-global dict 언급 제거 / 환경변수 절에 `REDIS_URL`).
- [ ] 새 env var 1개 (`REDIS_URL`) 외 추가 env / migration / 외부 서비스 의존 없음. feature flag 0개.

---

## Implementation Plan Outline (informative — formalized in plan.md)

1. **헬퍼 모듈** (`app/infrastructure/cache/chat_state.py`, NEW): `redis.asyncio` import + module-level `_pool: Redis | None = None` lazy + `_get_pool() -> Redis | None` (fail-open) + 5개 async 헬퍼(`get_cursor`, `set_cursor`, `is_logged`, `mark_logged`, `clear_logged`) — 모두 try/except 로 swallow.
2. **config 필드** (`app/core/config.py`, MODIFIED): `REDIS_URL: str = "redis://localhost:6379/1"` 신규.
3. **lifespan warm** (`app/main.py`, MODIFIED): startup 에 `_get_pool()` 호출 + `PING` 시도 → 성공 시 `logger.info` "redis pool warmed (url=redis://****/1)", 실패 시 `logger.info` "redis warm skipped (fail-open): <reason>". shutdown 에 pool `aclose()`.
4. **respond.py 교체** (MODIFIED, REQ-CHAT-STATE-001 + REQ-CHAT-STATE-002): module-global `_CARD_BATCH_CURSOR` / `_LOGGED_IMPRESSION_IDS` / `reset_card_batch_cursor_for_tests` 삭제. `send_hybrid_batch` 의 cursor read/write 2 지점을 `await get_cursor(chat_id)` / `await set_cursor(chat_id, next_offset)` 으로 교체. `_log_delivered_impressions` 의 dedupe 4 지점을 `await clear_logged` / `await is_logged` / `await mark_logged` 로 교체. id-less candidate 의 `fresh.append` 동작은 byte-identical 보존.
5. **pyproject.toml** (MODIFIED): `[project.dependencies]` 에 `"redis>=5.0"` 추가, `[dependency-groups.dev]` 에 `"fakeredis>=2.0"` 추가.
6. **docker-compose.yml** (MODIFIED, 로컬): `services.redis: {image: redis:7-alpine, ports: ["6379:6379"]}` + `services.ai-server.depends_on: [redis]` + `services.ai-server.environment.REDIS_URL: redis://redis:6379/1`.
7. **aws-infra docker-compose** (MODIFIED, prod): dev-ai 의 `ai-server` 서비스 환경변수 추가 `REDIS_URL=redis://:${REDIS_AUTH}@redis:6379/1`. memory "dev-ai Telegram setup" 패턴 — tracked infra .env 가 키를 보유.
8. **테스트** (NEW): `tests/test_infrastructure/cache/test_chat_state.py` ~12 케이스 + `tests/test_agents/tools/test_respond_redis_integration.py` ~6 케이스. fakeredis fixture 는 `conftest.py` 에 정의(function scope, 헬퍼 모듈의 `_pool` 을 monkeypatch).
9. **CLAUDE.md 갱신**: 핵심 파일 표 + 환경 변수 절.
10. **회귀 grep**: `_CARD_BATCH_CURSOR`, `_LOGGED_IMPRESSION_IDS`, `reset_card_batch_cursor_for_tests` 검색이 빈 결과인지 PR 머지 전 확인.
11. **수동 smoke**: 로컬 docker-compose → 봇 시나리오 → redis CLI 키 확인 → dev-ai 배포 → 동일 시나리오 + `ai.card_impression` 중복 row 검증.

---

## Test Plan Outline (informative — formalized in acceptance.md)

- **Unit (`tests/test_infrastructure/cache/test_chat_state.py`)**: 5개 헬퍼 × happy path (cursor set/get, set member add/check, set clear) + 5개 헬퍼 × fail-open (redis client raises ConnectionError/TimeoutError → 안전 default 반환 + DEBUG 1줄) + TTL 검증(`ttl()` 양수) × 2 (cursor 키 / imp 키).
- **Unit / Integration (`tests/test_agents/tools/test_respond_redis_integration.py`)**: fakeredis fixture 위 `send_hybrid_batch` 호출 시 cursor 정확히 set(offset=0 진입 후 `kiko:cursor:{chat_id}` 가 next_offset 으로 set), 후속 offset=None 호출이 그 cursor 를 read; 5개 candidate 발사 후 `kiko:imp:{chat_id}` SET 멤버 = 5 product_id, 같은 5개 재발사(`is_fresh_search=False`) 시 `log_impressions` INSERT 호출 0건 + set 멤버 그대로; 새 검색(`is_fresh_search=True`) 시 `kiko:imp` DEL 후 새 5개 등록; id-less candidate 1개 + 정상 4개 시 fresh 길이 5 + 정상 4개만 set 멤버.
- **Regression**: 기존 cursor/impression 관련 테스트(있다면)가 새 헬퍼로 마이그레이션되어 모두 green. AST/grep: 두 모듈 글로벌 + reset 함수 literal 이 코드에서 사라짐.
- **Manual smoke**: 로컬 docker-compose smoke (DoD 항목) + dev-ai prod smoke (DoD 항목).
