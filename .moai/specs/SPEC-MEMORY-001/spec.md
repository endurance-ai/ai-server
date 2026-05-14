---
id: SPEC-MEMORY-001
version: 1.1.0
status: draft
created: 2026-05-11
updated: 2026-05-14
author: hchsa77@gmail.com
priority: P0
issue_number: null
---

# SPEC-MEMORY-001: Postgres-Backed Memory Persistence for Session & Taste Profile

## HISTORY

- 2026-05-14 (v1.1.0): TasteProfileStore Protocol extended with `seed_from_onboarding(user_key, weights: dict)` method per SPEC-ONBOARD-CARDS-001 REQ-ONBOARD-SEED-001 / REQ-ONBOARD-MEMORY-AMEND-001. REQ-MEMORY-PROTOCOL-001's "Protocol surface SHALL be unchanged" promise is hereby amended: surface is **additive-only** — new methods MAY be added with default no-op fallback on Protocol implementations. The previously frozen Protocol method set (`get_or_create`, `update`, `delete`, `lock_for`) remains unchanged in signature and semantics. New methods MUST be added with default implementations on the Protocol (or a documented contract requiring concrete classes to implement them, with a fallback path on the in-memory tier). This amendment was prerequisited by SPEC-ONBOARD-CARDS-001 v0.3.0 (Cross-SPEC Amendments section) and lands BEFORE any code commit adding `seed_from_onboarding`.
- 2026-05-11 (v0.2.0): plan-auditor 1차 감사(0.62) 반영. 7가지 결함 수정. **Blockers**: (D1) `delete()` Protocol 의미 명시를 위해 REQ-MEMORY-PROTOCOL-002 신설; (D2) lazy TTL 경합 제거를 위해 REQ-MEMORY-SESSION-002 의 OR 조항을 단일 atomic `INSERT ... ON CONFLICT DO UPDATE WHERE` 로 확정. **Majors**: (D3) `MEMORY_FALLBACK_ON_PROBE_FAIL` 운영 안전을 위해 REQ-MEMORY-FALLBACK-002 신설 (prod=`false` 강제); (D4) REQ-MEMORY-PERSIST-002 round-trip 에 `last_active` 명시적 포함 (`timestamptz(6)` 마이크로초 정밀도 + epoch ↔ timestamp 변환 계약); (D5) Background "multi-worker 자연 안전" 주장을 "last-write-wins, not serializable" 로 정정하고 Non-Goal #9 와 일치시킴; (D6) testcontainers-python 채택 확정 + dev-deps 명시; (D7) `/health/ready` backend 필드를 위해 REQ-MEMORY-HEALTH-001 신설 + `app/api/health.py` Affected Modules 등재. **Minors**: (D8) `last_results` JSON 인코더 cascade 를 REQ-MEMORY-SESSION-001 에 정식화; (D9) probe 타임아웃 측정 방법 (`psycopg.connect_timeout` + hanging-server mock) 명시; (D10) probe SQL 을 `SELECT 1 FROM user_taste_profile LIMIT 0` 로 확정하고 fresh-DB tradeoff 를 R10 에 문서화; (D11) 포팅 대상 테스트 (`tests/test_taste_profile.py`, `tests/test_graph_state.py`) 열거; (D12) 신규 모듈 85%+ 커버리지 목표 추가 (TRUST 5 Tested); (D13) DoD (d) 를 binary-testable 하게 강화; (D15) `testcontainers[postgres]` 를 dev deps 에 추가.
- 2026-05-11 (v0.1.0): 초안 작성. `docs/research/conversational-shopping-agents.md` takeaway #4 ("agentic 베이스라인은 영속 메모리를 전제로 한다")를 충족하기 위해 `InMemorySessionStore` / `InMemoryTasteProfileStore` 를 Postgres 백엔드로 교체. 기존 `SessionStore` / `TasteProfileStore` Protocol은 변경하지 않고 새 구현체만 추가하는 zero-touch 방식. dev-app Postgres (자체호스팅, `DB_URL` 직결, PostgREST shim 우회) 를 마이그레이션 도구는 Alembic으로 결정. 후속 SPEC인 SPEC-IMPLICIT-FB-001 (카드 노출/암묵 피드백) 과 SPEC-OBSERVABILITY-002 (Langfuse 활성화) 의 토대가 된다.

---

## Goal

현재 텔레그램 패션 봇의 **상태(working memory)** 는 두 개의 in-memory 딕셔너리에 들어 있다:

- `app/channels/session.py::InMemorySessionStore` — `chat_id → Session` (대화 진행 상태: vision 결과, clarify trail, last_results, sticky lang, …)
- `app/channels/taste_profile.py::InMemoryTasteProfileStore` — `user_key → TasteProfile` (감쇠 가중치 기반 brand/keyword 선호도, price 관측 범위)

이 구조는 컨테이너가 한 번 재기동되면 모든 사용자의 학습된 선호도와 진행 중인 대화 컨텍스트가 통째로 사라진다. POC 단계에서는 감내해 왔지만 "agentic" 베이스라인을 갖추려면 **재시작 너머의 영속성** 이 전제 조건이다 (`docs/research/conversational-shopping-agents.md` takeaway #4).

이 SPEC은 두 store 를 **dev-app Postgres 16 직결** 백엔드로 옮긴다. `search_products_v5` RPC 용 nginx PostgREST shim 은 우회하고, 자체 메타데이터 쓰기에는 `psycopg[binary]>=3.2` (이미 의존성 트리에 존재) 의 async 풀을 직접 사용한다. 마이그레이션 도구는 **Alembic** 으로 고정 — 첫 revision 에서 `user_taste_profile` / `user_session` 두 테이블을 베이스라인으로 생성한다.

핵심 설계 원칙:

1. **Protocol surface 불변** — `SessionStore` / `TasteProfileStore` Protocol 의 메서드 시그니처는 한 글자도 바뀌지 않는다. LangGraph 노드와 채널 레이어는 코드 수정 없이 새 구현체를 받는다 (`set_store_factory` / `set_taste_store_factory` 주입 지점 활용).
2. **Graceful fallback** — Postgres 가 startup 시점에 도달 불가하면 `InMemoryStore` 로 자동 강등하고 에러 로그만 남긴다. 봇은 죽지 않는다 (Langfuse fallback 과 동일한 패턴).
3. **POC 단계 데이터 폐기** — 컷오버 시점의 in-memory 데이터는 마이그레이션하지 않는다. 사용자 수가 한 자릿수인 단계라 가치 대비 복잡도가 크다.
4. **JSONB 우선** — `liked_brands` / `disliked_brands` / `liked_keywords` / `disliked_keywords` 등 감쇠 가중치 dict 와 vision_result 등의 구조화 객체는 정규화하지 않고 JSONB 컬럼으로 그대로 저장. 향후 cross-user 집계가 필요해지면 별도 ETL view 로 분리한다 (이 SPEC의 범위 외).
5. **Lazy TTL** — 세션 만료는 in-memory 구현이 쓰던 백그라운드 evict 태스크 대신 `ttl_expires_at` 컬럼 + 읽기 시 lazy 만료 + 주기 cleanup job 하이브리드. taste profile 은 30일 TTL 이지만 사실상 활성 사용자는 갱신되므로 의미적으로는 hard expiry 가 아닌 staleness 마커에 가깝다.

이 마이그레이션은 **데이터 레이어 교체** 이지 그래프 토폴로지 변경이 아니다. 외부 행위 (사용자가 보는 메시지, 추천 결과, KO/EN sticky 언어, clarify 카드 흐름) 는 byte-identical 하게 유지된다.

이 SPEC은 **WHAT** 과 **WHY** 만 정의한다. 구체적인 Alembic env.py 구조, connection pool 설정 튜닝, cleanup job 의 스케줄 운영 등 **HOW** 는 `plan.md` 와 Run phase 에서 결정한다.

---

## Background

### 현재 상태 (in-memory)

- `InMemorySessionStore._sessions: dict[int, Session]` — single-process. uvicorn `--workers 1` 가정.
- `InMemoryTasteProfileStore._profiles: dict[str, TasteProfile]` — 동일한 가정.
- 두 store 모두 `start()` 시점에 백그라운드 evict 코루틴을 띄워 TTL 경과한 엔트리를 메모리에서 제거.
- 컨테이너 재기동 시 모든 상태 소실. 활성 대화는 IDLE 로 리셋되고, 학습된 brand 선호도는 0 에서 다시 시작.

### 영속화 동기

`docs/research/conversational-shopping-agents.md` takeaway #4 의 요약:
> "Agentic 베이스라인의 첫 번째 전제는 cross-session memory 이다. 세션 휘발 모델로는 'agent' 라고 부를 수 없다."

대화형 쇼핑 에이전트 문헌 (Mehrotra et al. 2024, Klick & Lewis 2025 등) 은 모두 **장기 사용자 상태** 와 **단기 대화 상태** 의 분리, 그리고 두 상태가 모두 영속이라는 점을 명시적 전제로 둔다. 우리의 `Session` ↔ `TasteProfile` 분리는 이미 그 구조를 따라가지만, 저장 매체가 휘발성이라 절름발이 상태다.

### dev-app Postgres 직결

SPEC-INFRA-MIGRATE-001 P6 컷오버 (2026-05-10) 이후 데이터베이스는 dev-app EC2 의 자체호스팅 Postgres 16 으로 일원화됐다. **읽기 RPC** (`search_products_v5`) 는 nginx PostgREST shim (`http://172.31.59.31:3001`) 을 거치지만, 우리 자체 메타데이터 쓰기는 그럴 필요가 없다 (PostgREST 의 RLS / 권한 모델은 product 카탈로그에는 맞지만 internal 메타데이터에는 과한 오버헤드). `DB_URL` 환경변수를 그대로 사용하되 PostgREST URL 대신 Postgres wire protocol URL (`postgresql://...`) 을 추가 env 로 분리한다 (`DB_DSN`).

### Migration tool 선택

- **Alembic** — SQLAlchemy 의 표준 마이그레이션 도구. async psycopg3 와 호환되며, 우리는 SQLAlchemy ORM 을 쓰지 않더라도 Alembic 단독으로 raw DDL revision 을 관리할 수 있다. 의존성: `uv add alembic`.
- 대안 (raw SQL files + 자체 runner, sqlx-like 도구) 는 도구 자생 비용 대비 이점이 없어 기각.

### Concurrency 모델

현재 `--workers 1` 가정은 in-memory store 의 한계 때문이었다. Postgres 백엔드로 옮기면 **단일 row 쓰기 무결성** 은 `INSERT ... ON CONFLICT DO UPDATE` 의 원자성에 의해 확보된다. 다만 다음을 명확히 한다:

- **본 SPEC 의 동시성 보장 = last-write-wins, NOT serializable across workers.** 여러 워커가 같은 `chat_id` 에 대해 read-modify-write 를 수행할 때, application-level 직렬화는 보장되지 않는다 (`asyncio.Lock` 은 **프로세스 내부 only**). 두 워커가 동시에 `get_or_create → mutate → update` 사이클을 돌리면 한쪽의 변경이 다른 쪽 변경에 의해 덮어쓰일 수 있다.
- 이는 **의도된 한계** 다. Distributed locking (Postgres advisory lock, Redis Redlock 등) 은 Non-Goal #9 로 명시적으로 deferred. 본 SPEC 은 워커 수를 늘리는 것을 *목표로 삼지 않는다*.
- 후속 SPEC 에서 multi-worker 가로 확장이 필요해지면, advisory lock 도입 또는 `SELECT ... FOR UPDATE` 트랜잭션 패턴을 추가로 도입해야 한다.

따라서 **현 단계에서 Postgres 백엔드는 컨테이너 재기동 횡단 영속성을 제공하지만, 동시 워커 환경에서의 serializable 일관성은 제공하지 않는다.** 운영상 `--workers 1` 을 계속 사용한다는 전제는 유지된다.

---

## Architecture Snapshot (informative)

Today (in-memory):

```
LangGraph node ──┐
                 ├──► get_store() ──► InMemorySessionStore ──► dict[int, Session]
                 │                                              (process memory)
                 └──► get_taste_store() ──► InMemoryTasteProfileStore ──► dict[str, TasteProfile]
                                                                          (process memory)
```

After this SPEC (Postgres-backed):

```
                                                       ┌─► PostgresSessionStore
LangGraph node ──┐                                     │     │
                 ├──► get_store() ──────────────────── ┤     ├─► psycopg3 AsyncConnectionPool
                 │      (Protocol unchanged)           │     │
                 │                                     │     ▼
                 │                                     │   dev-app Postgres 16
                 │                                     │   (user_session table, JSONB)
                 │
                 └──► get_taste_store() ─────────────── ┤
                                                       │   ┌─► PostgresTasteProfileStore
                                                       │   │     │
                                                       └───┤     ├─► (same pool)
                                                           │     ▼
                                                           │   user_taste_profile table

Fallback path (startup probe fails):
    set_store_factory(InMemorySessionStore)  ─► identical to today
    set_taste_store_factory(InMemoryTasteProfileStore)
    log.error("Postgres unreachable; falling back to in-memory stores")
```

**Affected modules in kikoai/ai (this SPEC)**:

- `app/providers/db_pool.py` — NEW. psycopg3 `AsyncConnectionPool` 싱글톤 + lifespan integration.
- `app/channels/session_pg.py` — NEW. `PostgresSessionStore` 구현.
- `app/channels/taste_profile_pg.py` — NEW. `PostgresTasteProfileStore` 구현.
- `app/main.py` — lifespan 에서 pool 시작 / 종료, fallback factory 결정.
- `app/core/config.py` — 새 env vars (`DB_DSN`, `MEMORY_POOL_MIN_SIZE`, `MEMORY_POOL_MAX_SIZE`, `MEMORY_FALLBACK_ON_PROBE_FAIL`, `SESSION_CLEANUP_INTERVAL_S`).
- `app/api/health.py` — MODIFIED. `/health/ready` 응답에 `memory_backend: "postgres" | "in_memory"` 필드 추가 (REQ-MEMORY-HEALTH-001).
- `migrations/` — NEW. Alembic env + 첫 revision (두 테이블 생성).
- `alembic.ini` — NEW.
- `pyproject.toml` — main deps: `alembic` 추가. dev deps: `testcontainers[postgres]` 추가 (테스트 격리용 — Open Question 4 해결: testcontainers-python 채택).
- `tests/test_memory_pg/test_session_store.py` — NEW. characterization 테스트 (testcontainers Postgres 사용, ≥ 85% coverage on `app/channels/session_pg.py`).
- `tests/test_memory_pg/test_taste_store.py` — NEW (testcontainers, ≥ 85% coverage on `app/channels/taste_profile_pg.py`).
- `tests/test_memory_pg/test_fallback.py` — NEW (probe-failure paths, hanging-server simulation).
- `tests/test_memory_pg/test_db_pool.py` — NEW (≥ 85% coverage on `app/providers/db_pool.py`).
- `tests/test_memory_pg/test_health.py` — NEW (REQ-MEMORY-HEALTH-001 backend field assertions).
- **Ported tests (existing files re-run against both backends via fixture parametrization)**:
  - `tests/test_taste_profile.py` — currently only exercises `InMemoryTasteProfileStore`; re-parametrized to run against `PostgresTasteProfileStore` as well via a `store_backend` fixture (`["in_memory", "postgres"]`).
  - `tests/test_graph_state.py` — any test that constructs a `Session` and round-trips through `SessionStore` (currently in-memory only); re-parametrized identically. Tests that *only* exercise `Session` dataclass shape (no store I/O) remain unparametrized.

**Reused, untouched modules**:

- `app/channels/session.py` — `Session` dataclass, `SessionStore` Protocol, factory injection 모두 그대로. `InMemorySessionStore` 도 제거하지 않는다 (fallback 으로 살아남는다).
- `app/channels/taste_profile.py` — 동일.
- `app/graphs/**` — 12 노드 어떤 것도 수정하지 않는다 (Protocol 호출 시그니처 유지).
- `app/channels/factory.py`, `app/channels/adapter.py` — messenger 어댑터 무관.
- `app/pipeline/**` — 검색 파이프라인 무관.
- `app/providers/database.py` — PostgREST 경유 RPC 클라이언트는 그대로 유지 (읽기 전용 product RPC 용).

---

## Schema Reference (informative — formalized in REQ-MEMORY-MIGRATION-001)

### `user_taste_profile`

| Column | Type | Notes |
|---|---|---|
| `user_key` | `text` PRIMARY KEY | `"u:{from_user_id}"` 또는 fallback `"c:{chat_id}"`. 기존 `user_key_for()` 헬퍼 결과를 그대로 PK. |
| `liked_brands` | `jsonb NOT NULL DEFAULT '{}'::jsonb` | `{brand_lower: weight_float}` 감쇠 가중치 dict. `_DECAY=0.9` 매 reinforce 시 곱셈. |
| `disliked_brands` | `jsonb NOT NULL DEFAULT '{}'::jsonb` | 동일 형태. |
| `liked_keywords` | `jsonb NOT NULL DEFAULT '{}'::jsonb` | `{keyword_lower: weight_float}`. |
| `disliked_keywords` | `jsonb NOT NULL DEFAULT '{}'::jsonb` | 동일. |
| `price_min_observed` | `integer` | nullable. |
| `price_max_observed` | `integer` | nullable. |
| `last_active` | `timestamptz NOT NULL DEFAULT now()` | 매 `update()` 마다 `now()` 로 갱신. |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | row 갱신 시 자동. trigger or app-level. |

Indexes:

- `PRIMARY KEY (user_key)` — 단일 접근 패턴.
- `INDEX idx_taste_last_active (last_active)` — staleness 정리용.

### `user_session`

| Column | Type | Notes |
|---|---|---|
| `chat_id` | `bigint` PRIMARY KEY | Telegram chat id (signed 64-bit). |
| `state` | `text NOT NULL DEFAULT 'idle'` | `SessionState` enum 의 string 값. |
| `from_user_id` | `bigint` | nullable; taste profile lookup 용. |
| `image_url` | `text` | nullable. |
| `detected_items` | `jsonb NOT NULL DEFAULT '[]'::jsonb` | Vision 결과 items 리스트. |
| `selected_item_index` | `integer` | nullable. |
| `vision_keywords` | `jsonb NOT NULL DEFAULT '[]'::jsonb` | legacy minimal schema 호환 (`list[str]`). |
| `vision_item` | `text` | nullable; legacy minimal schema. |
| `vision_result` | `jsonb` | nullable; SPEC-VISION-UNIFY-001 v2 schema 전체. |
| `vision_selected_item_index` | `integer` | nullable. |
| `vision_outfit_style_node_primary` | `text` | nullable. |
| `vision_outfit_style_node_secondary` | `text` | nullable. |
| `vision_outfit_mood_tags` | `jsonb NOT NULL DEFAULT '[]'::jsonb` | `list[str]`. |
| `vision_outfit_gender` | `text` | nullable. |
| `user_intent` | `text` | nullable. |
| `last_results` | `jsonb NOT NULL DEFAULT '[]'::jsonb` | Candidate-like list (critique 콜백용 참조). |
| `shown_product_ids` | `jsonb NOT NULL DEFAULT '[]'::jsonb` | `list[str]`; 중복 노출 제거. |
| `last_critique_summary` | `text` | nullable. |
| `boost_keywords` | `jsonb NOT NULL DEFAULT '[]'::jsonb` | SPEC-CLARIFY-CARDS-001 sticky boost. |
| `clarify_axis` | `text` | nullable; 관측 로그용. |
| `clarify_value` | `text` | nullable. |
| `lang` | `text NOT NULL DEFAULT 'en'` | KO/EN sticky. |
| `last_active` | `timestamptz NOT NULL DEFAULT now()` | 매 `update()` 갱신. |
| `ttl_expires_at` | `timestamptz NOT NULL` | `last_active + SESSION_TTL_SECONDS` 로 매 update 시 재계산. lazy expiry 의 기준. |

Indexes:

- `PRIMARY KEY (chat_id)`.
- `INDEX idx_session_ttl (ttl_expires_at)` — cleanup job 의 `WHERE ttl_expires_at < now()` 스캔 효율.

JSONB 폴리시:

- 모든 list/dict 필드는 JSONB 로 저장; Python 측에서는 `json.dumps` / `json.loads` 없이 psycopg3 의 JSONB 자동 변환에 의존.
- `extra="forbid"` 와 같은 Pydantic 검증은 store 레이어 진입 시점에 의존 (Session 은 dataclass 이므로 별도 검증 레이어 없음 — 입력 검증은 LangGraph 노드 책임 그대로).

---

## Requirements (EARS)

### Persistence — Taste Profile (REQ-MEMORY-PERSIST-*)

#### REQ-MEMORY-PERSIST-001 — `PostgresTasteProfileStore.update()` SHALL write-through to Postgres [P0]

**WHEN** any code path calls `taste_store.update(profile)`,
**THE SYSTEM SHALL** persist the profile to the `user_taste_profile` row keyed by `profile.user_key` such that a subsequent `taste_store.get_or_create(profile.user_key)` from any process (including a process started AFTER a container restart) returns a `TasteProfile` whose `liked_brands` / `disliked_brands` / `liked_keywords` / `disliked_keywords` / `price_min_observed` / `price_max_observed` are bit-for-bit equal to what was last written.

**Acceptance**:

- An integration test writes a profile with `liked_brands={"ami": 1.0, "lemaire": 0.9}` and `liked_keywords={"oversized": 1.0}` via store A, closes A's pool, opens a fresh pool B, and asserts `get_or_create(user_key)` returns the same dicts.
- The write SHALL use `INSERT ... ON CONFLICT (user_key) DO UPDATE SET ...` so that concurrent first-creates do not collide.
- `last_active` SHALL be set to `now()` on every update; `updated_at` SHALL also be updated.

#### REQ-MEMORY-PERSIST-002 — Decay weights AND `last_active` SHALL survive serialization round-trip [P0]

**THE SYSTEM SHALL** preserve the following fields across the Python ↔ Postgres round-trip:

1. `liked_brands` / `disliked_brands` / `liked_keywords` / `disliked_keywords`: floating-point weight values with no loss beyond IEEE-754 float64 precision (round-trips through JSONB).
2. `price_min_observed` / `price_max_observed`: exact integer equality (nullable preserved).
3. `last_active`: stored as `timestamptz(6)` (microsecond precision). The Python-side representation is `float` (Unix epoch seconds). The store layer SHALL coerce `float → datetime(tz=UTC)` on write (`datetime.fromtimestamp(value, tz=timezone.utc)`) and `datetime → float` on read (`.timestamp()`). Round-trip precision contract: `abs(written - read) ≤ 1e-6` seconds.

**Acceptance**:

- A property-style test reinforces a brand 10 times (so the decay-multiplier chain produces values like `1.0 * 0.9^9`), writes, reads back, and asserts the in-memory dict and DB-loaded dict are equal under `math.isclose(rel_tol=1e-9)` for every weight value.
- A `last_active` round-trip test writes `time.time()`, reads back, and asserts `abs(written - read) ≤ 1e-6`.
- The cap behavior (`_MAX_BRANDS=50`, `_MAX_KEYWORDS=50`) SHALL be enforced *before* write (existing behavior in `_cap()`), not via a DB-side trigger.

#### REQ-MEMORY-PERSIST-003 — Stale taste profiles SHALL be reclaimable but NOT auto-evicted on read [P0]

**THE SYSTEM SHALL NOT** automatically delete a taste profile on read when its `last_active` exceeds `TASTE_PROFILE_TTL_SECONDS` (30 days by default). Stale profiles SHALL remain readable so that returning users keep their preferences; reclamation SHALL be opt-in via a future ad-hoc cleanup task (out of scope here).

**Acceptance**:

- An integration test sets a profile's `last_active` to 60 days ago (direct SQL), calls `get_or_create(user_key)`, and asserts the original `liked_brands` / `liked_keywords` are returned unchanged.
- The store SHALL still UPDATE `last_active` to `now()` whenever `update()` is called for that profile (re-activation).
- Note: this rule INTENTIONALLY differs from `InMemoryTasteProfileStore`'s background evict loop, which hard-deletes after 30 days. The new behavior is more user-friendly and matches the research finding that taste signal is durable.

---

### Persistence — Session (REQ-MEMORY-SESSION-*)

#### REQ-MEMORY-SESSION-001 — `PostgresSessionStore` SHALL persist the full `Session` dataclass [P0]

**WHEN** any code path calls `session_store.update(session)`,
**THE SYSTEM SHALL** persist all 23 fields of the `Session` dataclass listed in the Schema Reference such that a subsequent `session_store.get_or_create(chat_id)` from any process returns a `Session` whose fields are equal (after JSONB round-trip) to what was last written.

**Acceptance**:

- An integration test populates a session with non-trivial values in every field (including `vision_result` as a nested dict, `last_results` as a list of dicts, `lang="ko"`, `state=SessionState.RESULTS_SENT`), writes, reads via a fresh pool, and asserts equality field-by-field.
- The store SHALL use `INSERT ... ON CONFLICT (chat_id) DO UPDATE SET ...`.
- `vision_result` SHALL be persisted as JSONB. The store layer SHALL NOT attempt to reconstruct it as a `VisionResult` Pydantic model on read — it returns the raw dict in `Session.vision_result: Any`, matching the existing dataclass typing.
- `last_results` and other heterogeneous JSON-typed fields SHALL be encoded via a single helper `_to_jsonable(value)` with this cascade (in order):
  1. If `value` has `.model_dump(mode="json")` (Pydantic v2 model), use it.
  2. Else if `value` is a `dataclass` instance (`dataclasses.is_dataclass(value)` and not a type), use `dataclasses.asdict(value)`.
  3. Else if `value` is `list` / `tuple`, recurse element-wise.
  4. Else if `value` is `dict`, recurse on values.
  5. Else fall back to `json.dumps(value, default=str)` (last-resort string coercion for `datetime` / `Decimal` / etc.).
  A characterization test feeds the actual `Candidate` shape from `app/pipeline/state.py` into `last_results` and asserts round-trip equality after JSONB persistence.

#### REQ-MEMORY-SESSION-002 — Sessions SHALL expire lazily at `ttl_expires_at` boundary via a SINGLE atomic statement [P0]

**WHEN** `session_store.get_or_create(chat_id)` is called,
**THE SYSTEM SHALL** execute a **single atomic SQL statement** that:

1. If no row exists for `chat_id`: INSERT a fresh default row.
2. If a row exists AND `ttl_expires_at >= now()`: return it as-is.
3. If a row exists AND `ttl_expires_at < now()`: **replace it in place** with a fresh default row (atomic update of all fields back to dataclass defaults, including `state='idle'`, `last_results='[]'`, etc., and recompute `ttl_expires_at = now() + SESSION_TTL_SECONDS`).

The canonical implementation SHALL be a single `INSERT ... ON CONFLICT (chat_id) DO UPDATE SET ... WHERE user_session.ttl_expires_at < EXCLUDED.created_at ... RETURNING *` pattern, OR equivalently a `WITH expired AS (DELETE ... WHERE ttl_expires_at < now() RETURNING chat_id) INSERT ... ON CONFLICT DO NOTHING ... RETURNING *` CTE. The chosen variant SHALL be a SINGLE round-trip and SINGLE statement — **NOT** "DELETE then INSERT" as two operations, and **NOT** "OR update in place" as an unconstrained either-or. Two concurrent expired reads SHALL deterministically collapse to one row.

**Acceptance**:

- An integration test writes a session, manually sets `ttl_expires_at` to 1 second in the past, calls `get_or_create(chat_id)`, and asserts a fresh `IDLE` session is returned.
- The same integration test asserts exactly one row exists for that `chat_id` after the call (no orphans, no duplicates), and all fields are at dataclass defaults.
- A concurrency test fires 10 simultaneous `get_or_create(chat_id)` calls on an expired row from a single pool; afterwards exactly one row exists and no `UniqueViolation` was raised. (This validates the atomic-statement claim.)
- `update()` SHALL recompute `ttl_expires_at = now() + SESSION_TTL_SECONDS` on every write.
- A periodic cleanup job (background asyncio task started in `start()`) SHALL run every `SESSION_CLEANUP_INTERVAL_S` (default `300` seconds) and execute `DELETE FROM user_session WHERE ttl_expires_at < now()` for storage hygiene. Correctness MUST NOT depend on this job — the per-read atomic statement already handles expiry. The cleanup job is purely to reclaim disk for sessions that are never read again.

---

### Migrations (REQ-MEMORY-MIGRATION-*)

#### REQ-MEMORY-MIGRATION-001 — Alembic SHALL be the migration tool with a baseline revision creating both tables [P0]

**THE SYSTEM SHALL** introduce Alembic as the project's database migration tool. The first revision SHALL create both `user_taste_profile` and `user_session` tables with the schema documented in the Schema Reference section, including primary keys and indexes.

**Acceptance**:

- `pyproject.toml` declares `alembic` in the main dependency group (`uv add alembic` was executed).
- `alembic.ini` exists at project root with `script_location = migrations`.
- `migrations/env.py` reads `DB_DSN` from `app.core.config.settings` and configures the SQLAlchemy URL accordingly. Offline mode SHALL also be supported (so CI can dry-run `alembic upgrade head --sql`).
- `migrations/versions/<rev>_create_memory_tables.py` exists. Running `alembic upgrade head` on a clean Postgres instance creates both tables with the documented columns, types, defaults, and indexes.
- Running `alembic downgrade base` cleanly removes both tables.
- The Alembic baseline revision SHALL be idempotent under `IF NOT EXISTS` semantics where the tool allows (raw `op.execute` if needed) so a re-run on a partially-migrated dev DB does not crash.
- A CI step (or pre-deploy hook — `plan.md` decides exact integration point) runs `alembic upgrade head` against the dev-app Postgres before the new code is deployed.

---

### Protocol Compatibility (REQ-MEMORY-PROTOCOL-*)

#### REQ-MEMORY-PROTOCOL-001 — `SessionStore` / `TasteProfileStore` Protocol surface SHALL be unchanged [P0]

**THE SYSTEM SHALL** introduce `PostgresSessionStore` and `PostgresTasteProfileStore` as new implementations of the existing `SessionStore` and `TasteProfileStore` Protocols defined in `app/channels/session.py` and `app/channels/taste_profile.py`. The Protocol classes themselves, the `Session` and `TasteProfile` dataclasses, and the `set_store_factory` / `set_store` / `get_store` / `init_store` / `shutdown_store` / `set_taste_store_factory` / `set_taste_store` / `get_taste_store` / `init_taste_store` / `shutdown_taste_store` function signatures SHALL NOT change.

**Acceptance**:

- A diff of `app/channels/session.py` and `app/channels/taste_profile.py` between this SPEC's start and end state shows ZERO modifications to the Protocol definitions, dataclasses, or factory-injection function signatures (additions to module-level docstrings noting Postgres availability are permitted).
- All existing LangGraph node call sites (`session.update(sess)`, `taste.reinforce_liked_brand(...)`, etc.) continue to work without modification.
- `lock_for(chat_id)` continues to return an `asyncio.Lock` (the Postgres impl uses an in-process lock map for serialization within a single worker — see Concurrency 모델 section and Non-Goal #9; cross-worker serializable locking is NOT delivered by this SPEC).
- An existing test that injects an `InMemorySessionStore` via `set_store(...)` for a unit test SHALL continue to work unchanged.

#### REQ-MEMORY-PROTOCOL-002 — `delete()` SHALL be idempotent and concurrent-safe in both backends [P0]

Both `SessionStore.delete(chat_id)` (in `app/channels/session.py`) and `TasteProfileStore.delete(user_key)` (in `app/channels/taste_profile.py`) are part of the unchanged Protocol surface. Their persistence semantics under the Postgres backend SHALL be:

**WHEN** `delete(pk)` is called,
**THE SYSTEM SHALL** execute a single statement `DELETE FROM <table> WHERE <pk_column> = $1`. The operation SHALL:

1. Be **idempotent**: calling `delete(pk)` on a row that does not exist (or has already been deleted) SHALL succeed silently — no exception is raised, no log line at WARN level or above. The DELETE statement's `0 rows affected` outcome is a valid success.
2. Be **concurrent-delete safe**: two simultaneous `delete(pk)` calls for the same key SHALL both return successfully; the underlying `DELETE` is naturally idempotent under Postgres MVCC.
3. **NOT cascade**: there are no foreign-key relationships from these two tables to others within this SPEC's scope. A future SPEC (e.g., SPEC-IMPLICIT-FB-001 `card_impression`) that adds child rows is responsible for its own cascade or cleanup policy.
4. **Take the in-process `asyncio.Lock` for that pk** during the DELETE (just like `update`), so that a concurrent `update` from the same worker does not race with the delete and resurrect a stale row.

**Acceptance**:

- An integration test calls `session_store.delete(chat_id)` on a non-existent `chat_id` and asserts no exception is raised.
- An integration test calls `session_store.delete(chat_id)` twice in a row on a real row and asserts (a) the row is gone after the first call, (b) the second call returns silently.
- A concurrency test fires `delete(pk)` + `update(session)` for the same `chat_id` from the same worker concurrently; the final state is either "row absent" or "row present at update's values", never partial / corrupted.
- A DoD checkbox below covers this REQ explicitly.

---

### Lifecycle (REQ-MEMORY-LIFECYCLE-*)

#### REQ-MEMORY-LIFECYCLE-001 — Connection pool SHALL be initialized in FastAPI lifespan and shut down cleanly [P0]

**WHEN** the FastAPI app starts (`app/main.py` lifespan enter),
**THE SYSTEM SHALL** open a `psycopg.AsyncConnectionPool` against `DB_DSN` with `min_size=MEMORY_POOL_MIN_SIZE` (default `2`) and `max_size=MEMORY_POOL_MAX_SIZE` (default `10`), probe connectivity with `SELECT 1 FROM user_taste_profile LIMIT 0` (resolved per Open Question 5 — this also confirms the table exists, catching "code deployed but migration not run" at startup), and only then register `PostgresSessionStore` / `PostgresTasteProfileStore` via the existing `set_store_factory` / `set_taste_store_factory` hooks.

**WHEN** the app shuts down (`app/main.py` lifespan exit),
**THE SYSTEM SHALL** stop the cleanup task, close all open store-side resources, and call `pool.close()` (or the psycopg3 equivalent) so all connections are returned and the pool is drained.

**Acceptance**:

- An integration test exercises full lifespan: startup → one round of `get_or_create` + `update` for each store → shutdown. The test asserts the pool reports `closed` after shutdown.
- The lifespan SHALL run pool initialization BEFORE messenger adapter warmup so the bot does not start accepting webhooks until persistence is ready.
- The probe SHALL complete within ≤ 5 seconds (wall clock). The timeout SHALL be enforced via psycopg's `connect_timeout` parameter (passed to `AsyncConnectionPool(conninfo=...)` as `connect_timeout=5`) AND an outer `asyncio.wait_for(probe(), timeout=5.0)` wrapper. A unit test SHALL simulate a hanging server (e.g., bind a TCP socket that accepts the connection but never replies to `startup` packet) and assert the lifespan completes within ≤ 6 seconds with the fallback path triggered.
- A `[MEMORY][startup]` log line SHALL announce the chosen backend ("postgres" or "in-memory fallback") so on-call has visibility from the boot log.

---

### Fallback (REQ-MEMORY-FALLBACK-*)

#### REQ-MEMORY-FALLBACK-001 — Postgres probe failure SHALL degrade gracefully to InMemory stores (when allowed by policy) [P0]

**WHEN** the startup probe fails for any reason (connection refused, timeout, auth error, unknown host, missing table) AND `MEMORY_FALLBACK_ON_PROBE_FAIL=true`,
**THE SYSTEM SHALL** log an `ERROR` level message, register `InMemorySessionStore` / `InMemoryTasteProfileStore` via the existing factories instead, and continue the FastAPI startup sequence so the bot remains operational.

**WHEN** the startup probe fails AND `MEMORY_FALLBACK_ON_PROBE_FAIL=false`,
**THE SYSTEM SHALL** re-raise the underlying exception from the lifespan, causing FastAPI startup to abort. The container's process manager (systemd / docker restart) is expected to handle the crash loop.

**Acceptance**:

- An integration test sets `DB_DSN` to a deliberately unreachable address (`postgresql://localhost:9/none`) with `MEMORY_FALLBACK_ON_PROBE_FAIL=true` and asserts:
  - The app starts successfully (no unhandled exception in lifespan).
  - `get_store()` returns an `InMemorySessionStore` instance.
  - `get_taste_store()` returns an `InMemoryTasteProfileStore` instance.
  - An `ERROR` log line containing `memory_backend=in_memory_fallback` is emitted with the underlying exception.
- A second integration test sets the same unreachable `DB_DSN` with `MEMORY_FALLBACK_ON_PROBE_FAIL=false` and asserts lifespan raises (FastAPI startup fails closed).
- The fallback is one-shot at startup; the system SHALL NOT attempt mid-flight reconnection to Postgres in this SPEC (deferred to a future SPEC if needed). Once degraded, restart is required to recover.

#### REQ-MEMORY-FALLBACK-002 — Production deployments SHALL set `MEMORY_FALLBACK_ON_PROBE_FAIL=false` [P0]

The default of `MEMORY_FALLBACK_ON_PROBE_FAIL=true` is **explicitly a dev-friendly default**, NOT a production-safe one. Silent degradation to in-memory storage in production would mask a real DB outage and lose user data without surfacing the failure to oncall.

**THE SYSTEM SHALL** treat this env var as environment-scoped policy:

| Environment | Required value | Rationale |
|---|---|---|
| Local development (`.env` / `docker compose up`) | `true` (default) | Bot stays operational when developer's local Postgres is down. |
| Dev EC2 (`dev-app`) | `true` (current default acceptable; POC stage, single-digit users) | Current operational reality. Re-evaluate when user count grows. |
| Production (when deployed) | `false` (REQUIRED) | Fail-closed semantics. A DB outage SHALL be loud, not silent. |

**Acceptance**:

- The dev-app deployment config (in `aws-infra/kiko-ai-servers/portal-ai/`) SHALL be reviewed in the cutover PR; if/when a production deployment exists, the production env file SHALL have `MEMORY_FALLBACK_ON_PROBE_FAIL=false` set explicitly (no reliance on default). Documented in `plan.md` cutover checklist.
- `.env.example` SHALL document this policy in a comment next to the variable: `# Set to "false" in production; "true" is dev-only.`
- A test SHALL assert the default value in `app/core/config.Settings` is `true` (the dev-friendly default), confirming we have NOT silently flipped the default and broken dev workflows.

---

### Observability (REQ-MEMORY-OBS-*)

#### REQ-MEMORY-OBS-001 — Postgres store write paths SHALL be wrapped with Langfuse `@observe` [P0]

**THE SYSTEM SHALL** decorate `PostgresSessionStore.update`, `PostgresSessionStore.get_or_create`, `PostgresTasteProfileStore.update`, and `PostgresTasteProfileStore.get_or_create` with the existing `@observe` decorator from `app/observability/langfuse.py` so that per-call latency is tracked when Langfuse is active, and is a no-op otherwise (consistent with the existing pattern).

**Acceptance**:

- A unit test against a Langfuse mock asserts that `update()` issues a span named `memory.session.update` (or analogous) with `chat_id` masked / hashed (the existing PII rule from SPEC-AGENT-001 REQ-OBSV-005 applies: no raw `chat_id`, no `from_user_id`).
- The `@observe` decoration SHALL include `as_type="span"` (not `"generation"`) since these are not LLM calls.
- When `@observe` is the no-op fallback (current state — langfuse v2 + langchain incompat), the decoration MUST NOT alter call semantics or add measurable latency.
- This requirement bridges into SPEC-OBSERVABILITY-002 (Langfuse activation) — when that SPEC lands, these spans light up automatically with zero additional code change.

---

### Health (REQ-MEMORY-HEALTH-*)

#### REQ-MEMORY-HEALTH-001 — `/health/ready` SHALL expose the active memory backend [P0]

The existing `/health/ready` endpoint (`app/api/health.py`) currently reports liveness + messenger adapter status. This SPEC extends the response with a `memory_backend` field so operators can detect degraded (in-memory fallback) state from a single probe.

**WHEN** a request is made to `GET /health/ready`,
**THE SYSTEM SHALL** include in the JSON response a field `memory_backend` whose value is one of:

- `"postgres"` — `PostgresSessionStore` / `PostgresTasteProfileStore` are registered (probe succeeded at startup).
- `"in_memory"` — `InMemorySessionStore` / `InMemoryTasteProfileStore` are registered (probe failed and fallback path is active).

**Acceptance**:

- An integration test starts the app with a reachable Postgres and asserts `GET /health/ready` returns `{"memory_backend": "postgres", ...}` (other existing fields preserved).
- An integration test starts the app with `MEMORY_FALLBACK_ON_PROBE_FAIL=true` and unreachable `DB_DSN`, and asserts `GET /health/ready` returns `{"memory_backend": "in_memory", ...}`.
- The detection logic SHALL inspect the registered factory (e.g., `isinstance(get_store(), PostgresSessionStore)`) rather than re-running the probe, so `/health/ready` remains cheap.
- The field is added in a backward-compatible way: existing fields (`status`, `messenger`, etc.) are untouched; consumers that ignore unknown fields continue to work.
- Scope creep acknowledged: this REQ adds `app/api/health.py` to Affected Modules. The change is small (one field) and operationally essential — without it, REQ-MEMORY-FALLBACK-001's "fail-loud on degraded state" promise is unobservable.

---

## Environment Variables (introduced or modified by this SPEC)

| Var | Required | Default | Description |
|---|---|---|---|
| `DB_DSN` | yes (for Postgres backend) | — | psycopg3-style DSN to dev-app Postgres, e.g. `postgresql://kiko:****@172.31.59.31:5432/kiko_dev`. Distinct from the existing `DB_URL` (which points at the nginx PostgREST shim for product RPC). REQ-MEMORY-LIFECYCLE-001. |
| `MEMORY_POOL_MIN_SIZE` | no | `2` | psycopg3 `AsyncConnectionPool` `min_size`. REQ-MEMORY-LIFECYCLE-001. |
| `MEMORY_POOL_MAX_SIZE` | no | `10` | psycopg3 `AsyncConnectionPool` `max_size`. REQ-MEMORY-LIFECYCLE-001. |
| `MEMORY_FALLBACK_ON_PROBE_FAIL` | no | `true` | When `true`, fall back to InMemory stores if Postgres probe fails. When `false`, fail startup. REQ-MEMORY-FALLBACK-001. |
| `SESSION_CLEANUP_INTERVAL_S` | no | `300` | Period (s) of the background cleanup task that deletes expired session rows. REQ-MEMORY-SESSION-002. |

Existing env vars consumed unchanged: `SESSION_TTL_SECONDS`, `TASTE_PROFILE_TTL_SECONDS` (used by the lazy cleanup path).

All new vars are read once at startup via `app/core/config.py::Settings` and exposed as typed properties. Restarts required for changes.

---

## Non-Goals (out of scope for this SPEC)

The following are explicitly NOT delivered by SPEC-MEMORY-001 and MUST NOT be conflated with it:

1. **Card impression / implicit feedback persistence.** Per-impression dwell / click signals belong in a dedicated `card_impression` table — that is SPEC-IMPLICIT-FB-001 (the immediate next SPEC). This SPEC only persists the two stores we already have.
2. **Langfuse callback handler activation.** `build_callback_handler` is currently `None` due to langfuse v2 + langchain incompat. Re-enabling Langfuse traces is SPEC-OBSERVABILITY-002. This SPEC decorates code with `@observe` so it is *ready* but does not turn it on.
3. **Multi-region replication / read replicas / failover.** Single dev-app Postgres instance. HA is a future infra SPEC, not a memory-layer concern.
4. **Cross-user analytics queries.** No ETL views, no analytics aggregation queries, no Materialized Views on `user_taste_profile`. JSONB is opaque-by-design here.
5. **Redis migration.** The earlier in-memory comment "post-demo migration target is Redis" is superseded by this SPEC. Postgres is the chosen path; Redis is rejected (single source of truth, no second store to operate, JSONB suffices for our shape).
6. **Schema migration of existing in-memory data.** The few active POC users will lose their in-memory state on cutover. No backfill / export script. This is an explicit POC-stage tradeoff.
7. **Encryption at rest of taste profiles.** dev-app Postgres uses standard EBS encryption. No column-level encryption for `liked_brands` etc. — these are not PII in the legal sense.
8. **Mid-flight reconnection to Postgres after startup fallback.** Once the bot starts in InMemory fallback mode, a restart is required. No "auto-retry every N seconds" logic.
9. **Cross-worker `lock_for(chat_id)` semantics.** Within a single uvicorn worker, the in-process `asyncio.Lock` map is preserved. Cross-worker concurrent writes to the same session row rely on Postgres row locks at the SQL layer (via `INSERT ... ON CONFLICT ... DO UPDATE`). Cross-worker distributed locking (e.g., advisory locks) is deferred until we actually need >1 worker.
10. **Changes to `Session` or `TasteProfile` dataclass shape.** Adding new fields to these classes is a separate concern. This SPEC persists exactly the fields that exist today.
11. **PostgREST schema sync with Alembic.** Our new tables are NOT exposed through the nginx PostgREST shim. The shim continues to serve only `products` / `search_products_v5` etc.
12. **Schema diff / migration drift detection in CI.** Alembic revisions are author-driven. No `alembic check` in CI yet (deferred).
13. **Graph node refactors.** Zero changes to `app/graphs/**`. Protocol-only swap.
14. **Tracing of read paths.** `get_or_create` is decorated for completeness, but read-path latency observability is not a primary goal — write paths are where Postgres I/O dominates.
15. **Per-user opt-out / GDPR delete.** No "forget me" endpoint. Deferred until a real user-facing privacy SPEC.

---

## Exclusions (What NOT to Build)

(Mirrors Non-Goals — explicit list for SPEC-checker compliance.)

1. No `card_impression` table. (→ SPEC-IMPLICIT-FB-001)
2. No Langfuse callback handler activation. (→ SPEC-OBSERVABILITY-002)
3. No multi-region replication or read replicas.
4. No cross-user analytics views or aggregation queries.
5. No Redis backend. Postgres is the chosen single-source-of-truth.
6. No data migration script from InMemory to Postgres on cutover.
7. No column-level encryption.
8. No mid-flight Postgres reconnection after a startup fallback.
9. No cross-worker distributed locking (Postgres row locks suffice for now).
10. No `Session` / `TasteProfile` shape changes.
11. No PostgREST exposure of the new tables.
12. No CI-level Alembic drift detection.
13. No LangGraph node code changes.
14. No read-path latency observability beyond span emission.
15. No user-facing privacy / deletion endpoints.

---

## Stakeholders

| Role | Responsibility |
|---|---|
| Product / Founder (hchsa77@gmail.com) | Approves the cutover-discards-data tradeoff (Non-Goal #6), the lazy-expiry-not-hard-evict policy for taste profiles (REQ-MEMORY-PERSIST-003), and the fallback-on-by-default policy (REQ-MEMORY-FALLBACK-001). |
| AI Server Owner (this SPEC) | All work in `app/providers/db_pool.py` (NEW), `app/channels/session_pg.py` (NEW), `app/channels/taste_profile_pg.py` (NEW), `app/main.py` lifespan, `app/core/config.py`, `migrations/` (NEW), `alembic.ini` (NEW), `pyproject.toml`. Owns Alembic baseline revision, fallback tests, characterization tests for both stores. |
| dev-app Postgres operator | Provisions a dedicated DB user with `INSERT / UPDATE / DELETE / SELECT` on `user_session` + `user_taste_profile` and `CREATE TABLE` for the Alembic upgrade. Verifies `pg_stat_activity` headroom for the new ~10-connection pool. |
| Langfuse operator | No action required for this SPEC. The `@observe` decorations are no-ops until SPEC-OBSERVABILITY-002 activates Langfuse. |
| Modal / kikoai/app teams | Out of scope. These tables are internal to kikoai/ai. |

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Postgres outage takes the bot down** even though in-memory fallback exists, because the probe times out long enough that webhooks back up. | Medium | High | REQ-MEMORY-LIFECYCLE-001 caps the probe at 5s. REQ-MEMORY-FALLBACK-001 puts the bot into in-memory fallback on probe failure. Combined, the bot starts within 5s even if Postgres is down. The cost is silent loss of persistence until restart — acceptable for dev. |
| R2 | **Schema drift between Alembic and what the Postgres store code expects.** A column rename in a revision without code update silently corrupts writes. | Medium | High | Integration tests in `tests/test_memory_pg/` run `alembic upgrade head` against a real (test-container or test DB) Postgres and exercise both stores end-to-end. CI runs them on every PR. Schema drift surfaces as a test failure. |
| R3 | **JSONB float precision loss** between Python `float` (IEEE-754 double) and Postgres `jsonb` (which stores numerics as text and reparses). | Low | Low | psycopg3's JSONB adapter round-trips floats via JSON, which is IEEE-754 compatible. REQ-MEMORY-PERSIST-002 has an explicit `math.isclose(rel_tol=1e-9)` test. Decay scores are inherently noisy — exact equality is not needed. |
| R4 | **Connection pool exhaustion** under burst traffic if `max_size=10` is too low. | Low | Medium | Default is conservative; can be tuned via `MEMORY_POOL_MAX_SIZE` env. Each LangGraph turn issues at most 2-3 store ops (session read, session write, taste read/write), so a single pool of 10 supports ~3-5 concurrent turns comfortably. At >5 concurrent turns we have bigger problems than the pool. |
| R5 | **`vision_result` JSONB writes are large** (full Vision v2 schema with style_node, sensitivity_tags, mood, palette, style, items[]). Repeated writes could bloat a row and slow updates. | Medium | Medium | The `vision_result` size is bounded by Vision LLM `max_tokens` (~1KB JSON typical). Postgres TOAST handles compression transparently. If we ever see slow updates, we can move `vision_result` to a sibling table (deferred). |
| R6 | **Alembic `env.py` reads `DB_DSN` from settings module** which transitively imports the full FastAPI app. CI / standalone migration runs get a fat import chain. | Medium | Low | `migrations/env.py` SHALL import only `app.core.config.settings`, not `app.main` or any other downstream module. Validated by a CI step (`uv run alembic check`) that times out if import takes >2s. |
| R7 | **`Session.last_results` carries Candidate-like objects** that may have non-serializable fields (e.g., decimal scores, datetime). | Medium | Medium | Store layer SHALL coerce via `json.dumps(default=str)` (or a more typed converter) on write. A characterization test feeds the actual `Candidate` shape from `app/pipeline/state.py` and asserts round-trip equality. |
| R8 | **Lazy-expiry race condition**: two concurrent `get_or_create` calls both see an expired row and both delete-then-insert. | Low | Low | The single Postgres operation we emit is `INSERT ... ON CONFLICT (chat_id) DO UPDATE` — atomic. Two concurrent inserts collapse to one row deterministically. No race. |
| R9 | **`user_session` row count grows unbounded** if the cleanup job fails silently. | Low | Low | Cleanup job emits `[MEMORY][cleanup]` INFO log every iteration with `expired_deleted=N`. On-call review weekly. At a 30-min TTL, ~2x daily-active users worth of rows at steady state — small. |
| R10 | **`MEMORY_FALLBACK_ON_PROBE_FAIL=true` masks a real production DB outage** — bot appears healthy, no persistence. ALSO: probe SQL `SELECT 1 FROM user_taste_profile LIMIT 0` will fail (and fall back) on a fresh DB where Alembic upgrade has not yet run — i.e., the system FALLS BACK rather than fails LOUDLY on "missing migration". | Medium | Medium | REQ-MEMORY-FALLBACK-001 emits ERROR log on every fallback. REQ-MEMORY-HEALTH-001 surfaces `memory_backend` on `/health/ready` — operators see degraded state at first probe. REQ-MEMORY-FALLBACK-002 mandates `MEMORY_FALLBACK_ON_PROBE_FAIL=false` in production, so the "fresh DB w/o migration" scenario in prod manifests as a startup crash loop (correct fail-closed). In dev, the same scenario manifests as in-memory fallback + ERROR log — developer sees it via `/health/ready` and runs `alembic upgrade head`. Tradeoff is explicit: dev convenience over fail-loud, prod fail-loud over uptime. |
| R11 | **`DB_DSN` exposed in error log message** during probe failure (e.g., psycopg's connection error includes the DSN). | Medium | Medium | The fallback log line SHALL log a *sanitized* DSN (host + port + db, no password). The probe-failure handler intercepts the exception, redacts, then logs. |
| R12 | **`asyncio.Lock` in-process lock map remains** even though Postgres row locks would suffice. Slight redundancy. | Low | Low | Acceptable — the in-process lock protects against intra-process contention where two coroutines for the same chat_id race. Cross-process safety comes from `INSERT ... ON CONFLICT`. Keeping the in-process lock match the existing Protocol surface (zero-touch). |

---

## Open Questions (deferred to plan.md / implementation)

Resolved in this revision (v0.2.0):

- ~~D8 / OQ3: `Session.last_results` JSON encoder.~~ → Resolved in REQ-MEMORY-SESSION-001 acceptance (5-step `_to_jsonable` cascade).
- ~~D6 / OQ4: Test infrastructure.~~ → Resolved: testcontainers-python adopted; `testcontainers[postgres]` listed in dev deps.
- ~~D10 / OQ5: Probe SQL.~~ → Resolved in REQ-MEMORY-LIFECYCLE-001: `SELECT 1 FROM user_taste_profile LIMIT 0`. Fresh-DB tradeoff documented in R10.

Remaining (legitimately plan.md / Run-phase decisions):

1. **Exact Alembic revision filename.** Lean `migrations/versions/0001_create_memory_tables.py`. `plan.md` confirms naming convention.
2. **Cleanup task connection acquisition pattern.** Per-iteration borrow from pool (lean) vs dedicated long-lived connection. `plan.md` decides based on idle-connection footprint vs reconnect cost analysis.
3. **`@observe` span naming convention.** Lean `memory.session.update`, `memory.session.get_or_create`, `memory.taste.update`, `memory.taste.get_or_create`. `plan.md` confirms exact strings (must be stable for Langfuse query/dashboard reuse once SPEC-OBSERVABILITY-002 lands).

---

## Cross-References

- **Builds on**:
  - SPEC-MSG-001 (channel transport, messenger adapter — unchanged).
  - SPEC-AGENT-001 (LangGraph 12-node topology — unchanged; only the store backing the nodes is swapped).
  - SPEC-PIPELINE-001 (search pipeline — unchanged; the new tables are independent of `search_products_v5`).
  - SPEC-VISION-UNIFY-001 (Vision v2 schema is what gets persisted in `user_session.vision_result`).
  - SPEC-AGENTIC-CRITIQUE-001 (critique trail and related state fields are part of the Session and will be persisted along with the rest of the dataclass).
  - SPEC-CLARIFY-CARDS-001 (`Session.boost_keywords`, `clarify_axis`, `clarify_value` persisted as documented).
- **Triggers / unblocks**:
  - SPEC-IMPLICIT-FB-001 (card impression table — directly depends on the Alembic baseline this SPEC creates).
  - SPEC-OBSERVABILITY-002 (Langfuse activation — leverages the `@observe` decorations this SPEC adds).
- **Affected modules in kikoai/ai**:
  - NEW: `app/providers/db_pool.py`, `app/channels/session_pg.py`, `app/channels/taste_profile_pg.py`, `migrations/env.py`, `migrations/versions/0001_create_memory_tables.py`, `alembic.ini`, `tests/test_memory_pg/` (test_session_store.py, test_taste_store.py, test_db_pool.py, test_fallback.py, test_health.py).
  - MODIFIED: `app/main.py` (lifespan), `app/core/config.py` (new env vars), `app/api/health.py` (new `memory_backend` field, REQ-MEMORY-HEALTH-001), `pyproject.toml` (main dep: `alembic`; dev dep: `testcontainers[postgres]`), `.env.example` (new env vars + production-policy comment on `MEMORY_FALLBACK_ON_PROBE_FAIL`).
  - PORTED (existing tests, re-parametrized to run against both backends): `tests/test_taste_profile.py`, `tests/test_graph_state.py` (store-touching subset).
  - UNCHANGED (asserted): `app/channels/session.py`, `app/channels/taste_profile.py` (Protocol + Session/TasteProfile dataclasses), `app/graphs/**`, `app/pipeline/**`, `app/providers/database.py` (PostgREST RPC client stays).
- **Project context**: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md`.
- **Research basis**: `docs/research/conversational-shopping-agents.md` takeaway #4.

---

## Definition of Done (P0)

- [ ] REQ-MEMORY-PERSIST-001 / 002 / 003 implemented. Taste profile writes survive a fresh-pool round-trip with exact dict equality; decay weights preserved under `math.isclose(rel_tol=1e-9)`; `last_active` round-trips with `abs(delta) ≤ 1e-6` seconds (microsecond precision via `timestamptz(6)`); stale-but-non-evicted policy in effect.
- [ ] REQ-MEMORY-SESSION-001 / 002 implemented. Full `Session` dataclass persists across processes via the `_to_jsonable` cascade; lazy TTL expiry handled by a single atomic statement (INSERT...ON CONFLICT...DO UPDATE...WHERE expired); concurrency test of 10 parallel expired-row reads collapses to exactly one row; background cleanup job removes expired rows for storage hygiene only.
- [ ] REQ-MEMORY-MIGRATION-001 implemented. `alembic.ini` + `migrations/env.py` + baseline revision present. `alembic upgrade head` creates both tables; `alembic downgrade base` drops them cleanly.
- [ ] REQ-MEMORY-PROTOCOL-001 implemented. Diff of `app/channels/session.py` and `app/channels/taste_profile.py` shows zero Protocol / dataclass / factory-signature changes.
- [ ] **REQ-MEMORY-PROTOCOL-002 implemented.** `delete()` is idempotent (no exception on missing pk), concurrent-delete safe (two parallel `delete(pk)` both succeed), and acquires the in-process `asyncio.Lock` to serialize against in-flight `update()` from the same worker.
- [ ] REQ-MEMORY-LIFECYCLE-001 implemented. Pool opens in lifespan with `min_size=2`, `max_size=10`; probe is `SELECT 1 FROM user_taste_profile LIMIT 0` with 5s `connect_timeout` + 5s `asyncio.wait_for` outer guard; hanging-server unit test asserts lifespan completes ≤ 6s with fallback triggered; pool closes cleanly on shutdown; `[MEMORY][startup]` log line announces backend.
- [ ] REQ-MEMORY-FALLBACK-001 implemented. Probe failure with flag=true ⇒ ERROR log + in-memory fallback; flag=false ⇒ lifespan raises (FastAPI startup fails closed). Sanitized DSN in the log line (no password).
- [ ] **REQ-MEMORY-FALLBACK-002 implemented.** Cutover PR / deployment runbook explicitly sets `MEMORY_FALLBACK_ON_PROBE_FAIL=false` for any production deployment; `.env.example` comment documents the policy; test asserts dev default remains `true`.
- [ ] REQ-MEMORY-OBS-001 implemented. Four `@observe` decorations present; span names finalized in `plan.md`; no PII (no raw chat_id / from_user_id) in span metadata.
- [ ] **REQ-MEMORY-HEALTH-001 implemented.** `GET /health/ready` returns `memory_backend: "postgres" | "in_memory"` field; integration tests cover both states; existing response fields preserved (backward-compatible).
- [ ] All existing tests (`pytest -q` baseline before this SPEC) continue to pass under both backends. Specifically: `tests/test_taste_profile.py` and `tests/test_graph_state.py` (store-touching subsets) re-run under a parametrized `store_backend` fixture (`["in_memory", "postgres"]`) and pass in both modes.
- [ ] **Coverage target (TRUST 5 Tested):** New modules `app/channels/session_pg.py`, `app/channels/taste_profile_pg.py`, `app/providers/db_pool.py` each report ≥ 85% line coverage in `pytest --cov`. `app/api/health.py` retains its existing coverage level (no regression).
- [ ] `app/core/config.py` and `.env.example` declare all 5 new env vars (`DB_DSN`, `MEMORY_POOL_MIN_SIZE`, `MEMORY_POOL_MAX_SIZE`, `MEMORY_FALLBACK_ON_PROBE_FAIL`, `SESSION_CLEANUP_INTERVAL_S`) with documented defaults; `MEMORY_FALLBACK_ON_PROBE_FAIL` entry has the production-must-be-`false` policy comment per REQ-MEMORY-FALLBACK-002.
- [ ] An end-to-end manual test against the dev Telegram bot exercises:
  - (a) Cold-start with Postgres healthy → user sends a photo → bot recommends → restart container → user replies in the same chat → bot remembers the previous `vision_result` and `boost_keywords`.
  - (b) User taste-update intent ("ami 좋아해") → restart → next search round shows that "ami" got boosted.
  - (c) Cold-start with Postgres unreachable (`DB_DSN` set to bad host) → bot starts in in-memory fallback → `/health/ready` returns `memory_backend: "in_memory"` → ERROR log line emitted with sanitized DSN.
  - (d) Two concurrent webhook requests for the same `chat_id` (replay test) → **exactly one row exists for that `chat_id` after both complete**; the row's `state` field equals one of the two writes' `state` values (last-write-wins is acceptable); no JSONB field contains a partial merge of both writes (each list/dict field is the complete value from exactly one of the two writes, not a spliced union); no `UniqueViolation` exception was raised.
- [ ] `ruff check . && ruff format --check .` passes.
- [ ] `pytest -q` passes at the same or higher count vs the pre-SPEC baseline; new test count includes ≥ 10 tests in `tests/test_memory_pg/` per file (session, taste, fallback, db_pool, health).

---

## Implementation Plan Outline (informative — formalized in plan.md)

1. **Dependency + scaffolding**: `uv add alembic`; create `alembic.ini`, `migrations/env.py` reading `DB_DSN`.
2. **Baseline revision**: `alembic revision -m "create memory tables"`, write DDL for both tables + indexes; `alembic upgrade head` against a local dev Postgres.
3. **Connection pool module**: `app/providers/db_pool.py` exposing `init_pool()`, `get_pool()`, `close_pool()` over `psycopg.AsyncConnectionPool`.
4. **Postgres store implementations**: `app/channels/session_pg.py`, `app/channels/taste_profile_pg.py` satisfying the existing Protocols. Use `INSERT ... ON CONFLICT ... DO UPDATE` on writes; lazy expiry on reads; JSONB on dict / list fields.
5. **Lifespan wiring**: `app/main.py` lifespan opens pool → probes → registers Postgres factories OR fallback factories → starts cleanup task → on shutdown reverses.
6. **Health endpoint**: `app/api/health.py::/health/ready` adds `backend` field.
7. **Tests**: testcontainers-based `tests/test_memory_pg/test_session_store.py`, `test_taste_store.py`, `test_fallback.py`. Plus a regression run of the existing test suite under both backends.
8. **Cutover**: alembic upgrade head on dev-app Postgres → deploy → smoke-test → monitor `/health/ready` for 24h.

---

## Test Plan Outline (informative — formalized in acceptance.md)

- **Unit (`tests/test_memory_pg/test_session_store.py`)**: `PostgresSessionStore` against a testcontainers Postgres. Cover `get_or_create / update / delete / lock_for / start / stop`. Includes the 23-field round-trip test (REQ-MEMORY-SESSION-001), the `_to_jsonable` cascade test for `last_results` with real `Candidate` shape, and the atomic lazy-expiry concurrency test (REQ-MEMORY-SESSION-002).
- **Unit (`tests/test_memory_pg/test_taste_store.py`)**: `PostgresTasteProfileStore` symmetric coverage. Decay-weight round-trip with `math.isclose(rel_tol=1e-9)`, `last_active` microsecond round-trip, 50-brand cap enforcement at write time.
- **Unit (`tests/test_memory_pg/test_db_pool.py`)**: pool open/close lifecycle, probe success/failure, hanging-server simulation for the 5s timeout AC.
- **Unit (`tests/test_memory_pg/test_fallback.py`)**: `MEMORY_FALLBACK_ON_PROBE_FAIL=true` and `=false` paths (REQ-MEMORY-FALLBACK-001 / 002).
- **Unit (`tests/test_memory_pg/test_health.py`)**: `/health/ready` returns the correct `memory_backend` value under both backends (REQ-MEMORY-HEALTH-001).
- **Integration**: cross-pool durability test (write via pool A, read via pool B); concurrent expired-row reads (10-way) collapsing to one row; delete idempotency under concurrent calls (REQ-MEMORY-PROTOCOL-002).
- **Ported tests (parametrized `store_backend` fixture)**:
  - `tests/test_taste_profile.py` — all tests run against both `InMemoryTasteProfileStore` and `PostgresTasteProfileStore`.
  - `tests/test_graph_state.py` — store-touching tests (those that invoke `set_store(...)` or `session_store.update(...)`) run against both backends; pure dataclass-shape tests unchanged.
- **Regression**: full existing `tests/` tree green under `MEMORY_FALLBACK_ON_PROBE_FAIL=true` (in-memory path) — so nothing else broke. Then full tree green under `MEMORY_FALLBACK_ON_PROBE_FAIL=false` + reachable testcontainers Postgres (Postgres path).
- **Coverage**: `pytest --cov=app.channels.session_pg --cov=app.channels.taste_profile_pg --cov=app.providers.db_pool` reports ≥ 85% per module.
- **End-to-end manual**: the four scenarios in the Definition of Done section.
