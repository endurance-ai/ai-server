---
id: SPEC-CONVERSATION-LOG-001
plan_version: 0.1.0
spec_version: 0.2.2
created: 2026-05-14
methodology: DDD (ANALYZE-PRESERVE-IMPROVE)
target_branch: feature/benchmark-noscroll
---

# Implementation Plan — SPEC-CONVERSATION-LOG-001 v0.2.2

> **Scope guard**: WHAT/WHY는 spec.md에서 잠긴 상태. plan.md는 **HOW**만 결정한다. SPEC에 정의된 19개 이벤트 타입, 4개 인덱스, REQ-LOG-* 카탈로그는 그대로 따른다. plan.md가 추가로 잠그는 결정은 SPEC의 Open Questions (OQ-1 ~ OQ-9) 해소만이다.

> **Methodology**: **DDD (ANALYZE-PRESERVE-IMPROVE)**. 본 SPEC은 12개 graph 노드 + webhook intake 라는 *기존 동작* 표면을 건드리므로 (`scope discipline` 위반 위험 R12 참조), 각 노드 수정 전에 characterization test를 먼저 박는다. 새 모듈 (`conversation_log.py`, `event_payloads.py`, migration `0003`) 만이 greenfield이고 거기는 SPEC-AGENT-001/SPEC-MEMORY-001 패턴을 그대로 따른다.

> **HARD prerequisite**: SPEC-MEMORY-001 v1.1.0 amendment는 commit `0c59e8b`로 이미 land. `ai` 스키마, `db_pool`, `MEMORY_BACKEND_IS_POSTGRES` flag 모두 사용 가능. 본 plan은 이 위에 쌓는다.

---

## 0. Assumption Audit

| # | Assumption | Confidence | Risk if wrong |
|---|---|---|---|
| A1 | `migrations/versions/`에는 현재 `0001_create_memory_tables.py`, `0002_create_card_impression.py`가 land됨. SPEC-ONBOARD-CARDS-001은 아직 land 전. → 본 SPEC revision = **`0003`** | High (verified via `ls migrations/versions/`) | SPEC-ONBOARD-CARDS-001이 먼저 머지되면 `0003`을 차지함. 본 SPEC은 그 경우 `0004`로 리넘버링 — branch 시작 시점에 한번 더 확인 (LOG-T01 첫 step). |
| A2 | `app/providers/db_pool.py`의 `get_pool() / sync→async loop bridge` 패턴 그대로 사용. 새 풀 X. | High (SPEC-MEMORY-001 명시) | 풀 동시 사용으로 인한 contention → `MEMORY_POOL_MAX_SIZE=10` 의 기본값이 충분한지 R1에서 이미 mitigation. |
| A3 | `app/observability/langfuse.py`에 `@observe` decorator + lifespan 초기화 패턴 존재. v3 `langfuse.Langfuse` 또는 `langfuse_context` 둘 중 하나로 `get_current_trace_id()` 가능 (실측 OQ-2 해소). | Medium | v3 API가 다르면 fallback cascade로 처리 — 본 plan §8에서 cascade 결정. |
| A4 | `app/graphs/state.py::WorkingState`는 Pydantic v2 + `extra="forbid"`. `thread_id: UUID` 추가 시 `extra="forbid"`와 부합 (field 추가는 OK, 외부에서 unknown key는 여전히 reject). | High (state.py inspected) | None. |
| A5 | testcontainers-postgres 는 SPEC-MEMORY-001로 이미 dev-deps. CI runner에서 docker 사용 가능. | High (`pyproject.toml` dev deps 명시) | CI에서 docker 미지원이면 `tests/test_conversation_log/` integration 테스트가 빈약해짐 — local-only로 운용. |
| A6 | `respond.py`, `send_results.py`, `evaluator.py`는 **반복 emit** 패턴 (chunk/card/iteration 당 1 row). 같은 `turn_no`를 공유한다는 REQ-LOG-TURN-001 컨벤션을 따른다. | High (SPEC §Event Type Catalog 명시) | None. |
| A7 | Code comment 언어는 파일별 surrounding style을 따른다. 신규 모듈 `conversation_log.py`, `event_payloads.py`는 English-leaning docstrings + bilingual inline comments (기존 SPEC-MEMORY-001 모듈 패턴 모방). | Medium | 충돌 시 PR 리뷰에서 조정. |

**Critical surfacing**: 위 가정 중 A1만 코드 시작 전에 한번 더 확인 필요 (re-list `migrations/versions/`). 나머지는 모두 검증 완료.

---

## 1. Module Structure — `app/observability/conversation_log.py`

### 1.1 공개 시그니처 (resolves OQ-4 / OQ-9)

```python
# app/observability/conversation_log.py
async def log_event(
    *,                                              # kwargs-only — SPEC 호환성
    event_type: str,                                # 19종 중 하나 (free-text 검증은 type checker만)
    user_key: str,                                  # "u:{id}" or "c:{id}"
    chat_id: int,
    thread_id: UUID,
    turn_no: int,
    payload: Mapping[str, Any],                     # truncation 적용 전 raw payload
    langfuse_trace: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Never raises. See REQ-LOG-FAILSOFT-001."""

def emit(
    *,
    event_type: str,
    user_key: str,
    chat_id: int,
    thread_id: UUID,
    turn_no: int,
    payload: Mapping[str, Any],
    latency_ms: int | None = None,
) -> None:
    """Caller-side fire-and-forget helper.

    1. `current_langfuse_trace_id()` 를 *caller 컨텍스트* 에서 즉시 호출 (R8).
    2. `_truncate(payload)` 적용.
    3. `asyncio.create_task(log_event(...))` + module-level WeakSet 보관 (R7).

    Never raises. Returns None (NOT asyncio.Task — call sites는 fire-and-forget 의도)."""
```

**Resolved OQ-4 / OQ-9** decisions:
- `log_event`는 `async def`, kwargs-only, 반환 `None`, 예외 미발생 (REQ-LOG-FAILSOFT-001).
- `emit`는 **동기 함수**. 내부에서 `asyncio.create_task` 호출 (이미 running event loop 가정 — webhook과 graph 모두 async 컨텍스트).
- `event_type`은 `str` (TypedDict 강제는 호출자 책임 + AST test가 catch — R14).
- `seed_thread() -> UUID`는 `uuid4` alias로만 export (SPEC § Affected modules).

### 1.2 내부 dispatch & failsoft 정책

```
emit(...)
  └─ langfuse_trace = current_langfuse_trace_id()    # caller context (R8)
  └─ payload = _truncate(payload)                    # REQ-LOG-PAYLOAD-CAP-001
  └─ task = asyncio.create_task(log_event(...))
  └─ _IN_FLIGHT: WeakSet[asyncio.Task] = set()       # R7 retention
       task.add_done_callback(_IN_FLIGHT.discard)
       _IN_FLIGHT.add(task)

log_event(...)
  ├─ if not MEMORY_BACKEND_IS_POSTGRES:
  │     log.debug("[CONV_LOG][skip] backend=in_memory event_type=%s", event_type)
  │     return  # REQ-LOG-FALLBACK-001
  │
  └─ try:
       pool = get_pool()                             # SPEC-MEMORY-001 re-use
       async with pool.connection() as conn:
           async with conn.cursor() as cur:
               await cur.execute(
                   "INSERT INTO ai.log_conversation_event "
                   "(user_key, chat_id, thread_id, turn_no, event_type, "
                   " payload, langfuse_trace, latency_ms) "
                   "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                   (user_key, chat_id, thread_id, turn_no, event_type,
                    Jsonb(payload), langfuse_trace, latency_ms),
               )
     except Exception as exc:                        # REQ-LOG-FAILSOFT-001
         _stderr_fallback(event_type=..., user_key=..., ..., error_phase=phase, exc=exc)
         return
```

**Pool reuse**: `from app.providers.db_pool import get_pool` (SPEC-MEMORY-001). 새 풀 절대 없음 (A2). `Jsonb` adapter는 psycopg3 표준.

### 1.3 `_to_jsonable` (resolves OQ-1)

SPEC-MEMORY-001 의 5-step cascade를 **import 재사용**. 새 헬퍼 만들지 않음 — 결합도가 한 방향(observability→channels)이고 cascade 로직은 SPEC-MEMORY-001에서 이미 검증됨. import 경로는 `app/channels/session_pg.py` 내부 헬퍼면 `_payload_to_jsonable`로 공개 export 추가 (한 줄 변경, 단순 가시성).

→ **결정**: `app/channels/session_pg.py`에서 `_to_jsonable` 헬퍼를 module-level `to_jsonable`로 rename + export (또는 새 `app/channels/_jsonable.py` 추출). plan.md가 후자 선택 — **신규 파일 `app/channels/_jsonable.py`**에 5-step cascade 이전, `session_pg.py` / `taste_profile_pg.py` / `conversation_log.py` 모두 거기서 import. 순환 의존 없음.

### 1.4 Truncation (REQ-LOG-PAYLOAD-CAP-001)

`_truncate(payload: Mapping[str, Any]) -> dict[str, Any]` — 작은 헬퍼 (≈40 LOC).

```
TEXT_CAP = 2048   # chars (REQ §1)
LIST_CAP = 50     # items
DICT_CAP = 100    # keys

def _truncate(p):
    out = {}
    for k, v in p.items():
        if isinstance(v, str):
            out[k] = v[:TEXT_CAP]
        elif isinstance(v, list):
            out[k] = [_truncate_item(x) for x in v[:LIST_CAP]]
        elif isinstance(v, dict):
            # if all values numeric → sort by value desc, take top 100
            # otherwise: arbitrary 100 key sample (sorted by key for determinism)
            out[k] = _truncate_dict(v)
        else:
            out[k] = v
    return out
```

Drop policy for dict: when values are numeric (e.g., `taste_update.keywords_delta` shapes), `sorted(items, key=lambda kv: kv[1], reverse=True)[:DICT_CAP]`; otherwise `sorted(items, key=lambda kv: kv[0])[:DICT_CAP]`. Silent — no `node_error` (REQ §4).

### 1.5 Stderr fallback (resolves OQ-5)

```python
def _stderr_fallback(*, event_type, user_key, chat_id, thread_id, turn_no,
                     payload, langfuse_trace, latency_ms, error_phase, exc):
    record = {
        "tag": "CONV_LOG_FALLBACK",
        "event_type": event_type,
        "user_key": user_key,
        "chat_id": chat_id,
        "thread_id": str(thread_id),
        "turn_no": turn_no,
        "payload": payload,                  # already truncated
        "langfuse_trace": langfuse_trace,
        "latency_ms": latency_ms,
        "error_phase": error_phase,          # "pool_acquire" | "insert" | "encode"
        "error_type": type(exc).__name__,
        "error_msg": str(exc)[:500],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)
```

**Required keys** (REQ-LOG-FAILSOFT-001 acceptance): `event_type`, `user_key`, `payload`, `error_phase`. Single-line JSON. Parseable by `json.loads`. Operator can `docker logs | jq 'select(.tag=="CONV_LOG_FALLBACK")'`.

---

## 2. Migration — `migrations/versions/0003_create_log_conversation_event.py`

Revision number = **`0003`** (verified via `ls migrations/versions/`: 0001, 0002 land됨; SPEC-ONBOARD-CARDS-001 미배포).

> **Re-check at branch start**: LOG-T01의 첫 step에서 `git log --all -- migrations/versions/` 로 다시 확인. SPEC-ONBOARD-CARDS-001이 머지된 상태라면 `0004`로 리넘버링 + `down_revision = "0003"`.

```python
"""create log_conversation_event table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-14

SPEC-CONVERSATION-LOG-001 / REQ-LOG-MIGRATION-001 — adds
`ai.log_conversation_event` with 10 columns + 4 indexes (1 GIN).
Idempotent under re-run.
"""

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"

def upgrade() -> None:
    op.execute("SET search_path TO ai")
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai.log_conversation_event (
            id              bigserial PRIMARY KEY,
            user_key        text NOT NULL,
            chat_id         bigint NOT NULL,
            thread_id       uuid,
            turn_no         integer,
            event_type      text NOT NULL,
            payload         jsonb NOT NULL,
            langfuse_trace  text,
            latency_ms      integer,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_log_conv_user_time "
               "ON ai.log_conversation_event (user_key, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_log_conv_thread "
               "ON ai.log_conversation_event (thread_id, turn_no)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_log_conv_event_type "
               "ON ai.log_conversation_event (event_type, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_log_conv_payload_gin "
               "ON ai.log_conversation_event USING GIN (payload jsonb_ops)")
    # jsonb_ops (default), NOT jsonb_path_ops — REQ-LOG-MIGRATION-001 acceptance

def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.log_conversation_event")
```

**No FK** (R3 — `user_session` row TTL과 독립). **No CHECK constraints on payload** (schema drift 자유, REQ-LOG-CATALOG-001 evolution).

---

## 3. WorkingState Extension — `app/graphs/state.py`

`InputState`에 두 필드 추가 (Pydantic v2 `Field(default_factory=...)`):

```python
class InputState(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    message: ChannelMessage
    chat_id: int
    from_user_id: int | None = None

    # NEW — SPEC-CONVERSATION-LOG-001 / REQ-LOG-THREAD-001 / REQ-LOG-TURN-001
    thread_id: UUID = Field(default_factory=uuid4)
    turn_no: int = 0
```

`WorkingState(InputState)`는 inherit하므로 자동 전파. 노드는 `state.thread_id` / `state.turn_no` 읽기 + `state.turn_no = N` 쓰기. LangGraph state reducer는 last-writer-wins이라 두 필드 모두 자연스럽게 propagate.

`OutputState`에 명시 X (graph 종료 후엔 의미 없음, SPEC §Architecture Snapshot 참조).

**propagation check**: webhook에서 `InputState(thread_id=resolved_tid, turn_no=0, ...)` 로 invoke. ingest에서 `turn_no=1` 셋 후 emit. resolve_image → 2, vision → 3, ... respond → 10.

---

## 4. Per-Node Emission Strategy (Highest-Risk Section)

각 노드는 try/except로 wrap한 단일 emit 추가. **노드 본문 변경 없음** (R12 scope discipline). 노드 익셉션 시 `node_error` emit + `recovered` 플래그.

### 4.1 Webhook intake (`app/api/webhooks/telegram.py`)

| Inbound type | Event | turn_no | Payload (truncation 후) |
|---|---|---|---|
| text Update | `user_text` | 0 | `{text, lang_detected}` |
| photo Update | `user_photo` | 0 | `{attachment_id, image_url, caption}` |
| callback Update | `user_callback` | 0 | `{callback_data, source_message_id}` |

**Thread_id resolution** (REQ-LOG-THREAD-CALLBACK-001 + REQ-LOG-THREAD-001):

```python
async def _resolve_thread_id(*, user_key, chat_id, update_type, source_message_id) -> UUID:
    """Returns (thread_id, prior_turn_no_for_callback_or_None)."""
    if update_type == "callback" and source_message_id is not None:
        async with pool.connection() as conn:
            row = await conn.execute(
                "SELECT thread_id, turn_no FROM ai.log_conversation_event "
                "WHERE user_key = %s "
                "  AND event_type = 'card_sent' "
                "  AND payload @> %s "
                "  AND created_at > now() - interval '30 days' "
                "ORDER BY id DESC LIMIT 1",
                (user_key, Jsonb({"source_message_id": source_message_id})),
                # source_message_id 는 user_callback payload (intake)에서 캡처
                # card_sent payload에서는 NOT stored — 그러나 send_results 노드에서
                # source_message_id 를 함께 emit 하도록 §4.10에서 추가.
            ).fetchone()
            if row:
                return row.thread_id  # turn_no override는 critique_apply에서 prior+1
    return uuid4()
```

**GOTCHA**: 위 SQL은 `card_sent.payload.source_message_id` 가 캡처되어 있어야 작동. SPEC §Event Type Catalog의 `card_sent` payload에는 `source_message_id`가 없음 (현 schema: `product_id, position, send_ok, send_elapsed_ms`). → **§4.10 send_results에서 `card_sent.payload`에 `source_message_id` 추가** (SPEC catalog 확장 — 본 plan이 OQ로 다루는 것이 아니라 SPEC catalog의 open-endedness 활용. v=1 묵시 유지, 새 키 추가는 schema 변경 불필요).

→ **Resolved**: send_results emit 시 `payload={"product_id":..., "position":..., "send_ok":..., "send_elapsed_ms":..., "source_message_id": telegram_msg_id}`. catalog 명시는 doc update에서 acceptance.md에 함께 기록.

**Performance** (REQ-LOG-THREAD-CALLBACK-001 < 50ms p99): 위 SQL은 `idx_log_conv_user_time` + GIN으로 cover. 사용자당 30일 row 수 ≈ 5K (10K turn/year × 0.5% callback-active user) — index scan으로 p99 < 20ms 예상. 50ms 미달 시 R16에서 Redis cache 추가 옵션.

### 4.2 `ingest.py` → `intent_routed` (turn_no=1)

기존 `intent_routed` decision은 `RoutedDecision` (app/channels/router.py)에 이미 있음. emit하면 됨:

```python
# at node end, AFTER decision is computed
state.turn_no = 1
emit(
    event_type="intent_routed",
    user_key=user_key_for(state),
    chat_id=state.chat_id,
    thread_id=state.thread_id,
    turn_no=state.turn_no,
    payload={
        "intent": decision.intent,
        "critique_delta_summary": decision.critique_delta_summary,  # nullable
    },
)
```

`node_error` wrapper: 노드 본문 전체를 `try / except Exception`. except 안에서 `emit(event_type="node_error", payload={"node_name":"ingest", ...})` + re-raise 결정.

### 4.3 `resolve_image.py` → `link_resolved` 또는 `pinterest_ingest` (turn_no=2)

분기:
- single image URL/photo → `link_resolved` `{input_url, resolved_image_url, host}`
- Pinterest board/profile/pin → `pinterest_ingest` `{mode, pin_count, vision_results_count}`

### 4.4 `vision.py` → `vision_done` (turn_no=3)

Payload는 v2 schema 전체 (SPEC §Catalog #6). state.vision_result에서 그대로 dump. `schema_v2_used`는 `state.vision_result is not None` 으로 결정. `error` field는 LLM 실패 시.

### 4.5 `pick_item.py` → `pick_item_done` (turn_no=4)

multi-item disambiguation 분기에서만 실행. auto_picked 케이스도 같은 노드.

### 4.6 `ask_clarify.py` → `ask_clarify_sent` (turn_no=5)

clarify 카드 표시 직후. `axis` + `options_shown` 캡처.

### 4.7 `apply_clarify.py` → `clarify_applied` (turn_no=동적)

callback 진입 시 turn_no=1 (callback이 새 webhook이지만 thread_id propagation 으로 같은 thread). `boost_keywords_added` 는 session.boost_keywords 누적분.

### 4.8 `search.py` → `search_done` (turn_no=6) — **ML 핵심**

```python
emit(
    event_type="search_done",
    ...
    payload={
        "query": {
            "text_query": query.text_query,
            "sparse_terms": query.sparse_terms,
            "embedding_present": embedding is not None,
            "filters": query.filters,
        },
        "embedding_ref": _sha256_prefix_16(embedding) if embedding else None,
        "top_k_product_ids": [c.product_id for c in raw_candidates],
        "rrf_scores": [c.rrf_score for c in raw_candidates],
        "dense_count": dense_n,
        "sparse_count": sparse_n,
        "filter_drop_log": filter_drops,  # list of {product_id, reason}
    },
)
```

`_sha256_prefix_16` (resolves OQ-7): `hashlib.sha256(embedding.tobytes()).hexdigest()[:16]`. Vector hashing 은 turn당 1회 (R13).

**Parallel-array invariant** (REQ-LOG-PAYLOAD-RICH-001): `assert len(top_k_product_ids) == len(rrf_scores)` BEFORE emit. 위반 시 `AssertionError` 잡혀 `node_error` 로 표면화.

### 4.9 `evaluator.py` → `evaluator_run` (turn_no=7, iteration당 1 row)

iteration loop 안에서 emit. 같은 turn_no=7을 공유 (REQ-LOG-TURN-001 monotonic non-decreasing). `iteration_no`가 distinguisher.

### 4.10 `send_results.py` → `diversify_done` + `card_sent` per card (turn_no=8 / 9)

```python
# diversify 직후
state.turn_no = 8
emit(event_type="diversify_done", ...,
     payload={"input_count":..., "output_count":..., "brand_cap":..., "platform_cap":...})

# 카드 전송 후
state.turn_no = 9
for i, card in enumerate(sent_cards):
    emit(
        event_type="card_sent",
        ...,
        payload={
            "product_id": card.product_id,
            "position": i,
            "send_ok": card.send_ok,
            "send_elapsed_ms": card.send_elapsed_ms,
            "source_message_id": card.telegram_message_id,  # NEW — §4.1 callback corr.
        },
    )
```

**기존 `ai.card_impression` INSERT는 무변경** (REQ-LOG-IMPLICIT-FB-COEXIST-001). 두 write는 독립.

### 4.11 `respond.py` → `bot_text` (turn_no=10, chunk당 1 row)

`_Flow` enum 값을 payload.flow로. KO/EN sticky lang 영향 없음 (text는 이미 결정됨).

### 4.12 `taste_update.py` → `taste_update` (turn_no=respond 이후 단계)

free_text 진입. `source="free_text"`. `keywords_delta` / `brands_delta` 는 노드 본체에서 이미 계산한 delta dict.

### 4.13 `critique_apply.py` → 두 가지 emit

- `crit:click:*` callback → `card_clicked` `{product_id, position, dwell_ms}` + `taste_update` `{source:"click", ...}`
- `crit:more/less/cheap` → `taste_update` `{source:"critique", ...}`

`card_clicked.thread_id` = REQ-LOG-THREAD-CALLBACK-001 에 따라 webhook intake에서 이미 propagate된 값.

### 4.14 `node_error` (모든 노드 공통 wrapper)

각 노드에 다음 패턴 추가:

```python
async def node_X(state):
    try:
        # existing logic + emit at end
        return state_delta
    except Exception as exc:
        emit(
            event_type="node_error",
            user_key=user_key_for(state),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=state.turn_no,
            payload={
                "node_name": "X",
                "exception_type": type(exc).__name__,
                "message": str(exc)[:500],
                "recovered": False,  # if re-raised; True if fallback path
            },
        )
        raise  # let LangGraph handle fallback routing
```

`recovered=True`는 노드별 catch 후 fallback path로 진행한 경우 (e.g., vision LLM 실패 → minimal schema fallback). 케이스별 결정은 노드 수정 PR에서 (특히 `vision.py`).

### 4.15 19 events 매핑표 (verification)

| # | Event | Emit site | turn_no |
|---|---|---|---|
| 1 | user_text | webhooks/telegram.py | 0 |
| 2 | user_photo | webhooks/telegram.py | 0 |
| 3 | user_callback | webhooks/telegram.py | 0 |
| 4 | intent_routed | nodes/ingest.py | 1 |
| 5 | link_resolved | nodes/resolve_image.py | 2 |
| 6 | vision_done | nodes/vision.py | 3 |
| 7 | pick_item_done | nodes/pick_item.py | 4 |
| 8 | ask_clarify_sent | nodes/ask_clarify.py | 5 |
| 9 | clarify_applied | nodes/apply_clarify.py | 1 (callback turn) |
| 10 | search_done | nodes/search.py | 6 |
| 11 | evaluator_run | nodes/evaluator.py | 7 (per iteration) |
| 12 | diversify_done | nodes/send_results.py (or 직전) | 8 |
| 13 | card_sent | nodes/send_results.py | 9 (per card) |
| 14 | card_clicked | nodes/critique_apply.py | 1 (callback turn) |
| 15 | onboard_select | (deferred — SPEC-ONBOARD-CARDS-001) | — |
| 16 | pinterest_ingest | nodes/resolve_image.py (분기) | 2 |
| 17 | bot_text | nodes/respond.py | 10 (per chunk) |
| 18 | taste_update | nodes/taste_update.py + nodes/critique_apply.py + channels/implicit_feedback.py | varies |
| 19 | node_error | 모든 12 노드의 except wrapper | varies |

---

## 5. Callback Thread Correlation (REQ-LOG-THREAD-CALLBACK-001)

§4.1에서 SQL과 thread_id resolution 흐름은 정의됨. 추가 세부:

### 5.1 Index strategy

- `idx_log_conv_user_time (user_key, created_at DESC)` — outer scan (user-key + 30-day window).
- `idx_log_conv_payload_gin (payload jsonb_ops)` — inner filter (`payload @> '{"source_message_id":N}'`).

EXPLAIN 예상: Bitmap Index Scan on payload_gin + Index Cond on user_time + Filter on event_type='card_sent'.

### 5.2 Failure mode

Lookup SQL 자체가 raise (DB transient error) → except로 catch → fresh `uuid4()` fallback (REQ-LOG-THREAD-001 path). 절대 webhook 차단 X.

### 5.3 Performance budget

| Metric | Target | Mitigation |
|---|---|---|
| p50 lookup | < 10ms | Index scan, single user partition |
| p99 lookup | < 50ms | R16 — 한계 도달 시 Redis cache `recent_card_sent[user_key] = (msg_id, thread_id, turn_no)` |
| Steady state | 1 lookup per callback Update | 사용자당 평균 1 callback/turn, ~10% turn에서 callback (R16에서 측정) |

---

## 6. Payload Truncation Wiring

**Centralized**: `emit(...)` 헬퍼 내부에서 `_truncate(payload)` 적용 (§1.4). 호출자(노드)는 raw payload만 전달. 노드별로 truncation 책임이 분산되는 것 방지 → DRY + 일관성.

| Field | Cap | Type | Drop policy |
|---|---|---|---|
| `user_text.text` | 2048 chars | str | tail truncate |
| `user_photo.caption` | 2048 chars | str | tail truncate |
| `bot_text.chunk_text` | 2048 chars | str | tail truncate |
| `intent_routed.critique_delta_summary` | 2048 chars | str | tail truncate |
| `link_resolved.input_url` | 2048 chars | str | tail truncate (실제 URL 길이 < 2048 보장) |
| `node_error.message` | 500 chars | str | tail truncate (기존 SPEC convention) |
| `search_done.filter_drop_log` | 50 items | list | tail drop |
| `clarify_applied.boost_keywords_added` | 50 items | list | tail drop |
| `vision_done.items` | 50 items | list | tail drop |
| `taste_update.keywords_delta.*` | 100 keys | dict | sort by value desc, take top |
| `taste_update.brands_delta.*` | 100 keys | dict | sort by value desc, take top |

Special-case `node_error.message`는 500 chars (SPEC §Catalog #19 명시). 일반 cap 2048과 충돌 → `_truncate` 안에 `event_type='node_error' AND key='message'` 특례 추가 (또는 호출자가 미리 500 cap 후 emit — 후자 선택, payload 명세에 가까움).

→ **결정**: 일반 cap은 `_truncate`가 적용. `node_error.message` 의 500-char cap은 호출자(except wrapper)에서 `str(exc)[:500]` 으로 명시적 적용 (§4.14 패턴).

---

## 7. Stderr Fallback Format (resolves OQ-5)

§1.5 참조. 결정:

- **Single-line JSON**, `\n` 종료.
- **Required keys**: `event_type`, `user_key`, `payload`, `error_phase`.
- **Recommended keys** (all included): `tag`, `chat_id`, `thread_id`, `turn_no`, `langfuse_trace`, `latency_ms`, `error_type`, `error_msg`, `ts`.
- **Prefix tag**: top-level `"tag": "CONV_LOG_FALLBACK"` field (NOT a prefix string — JSON-native filter via `jq` easier than regex).
- **No log level prefix** (e.g., no `WARN:`). stderr line is the record itself.

Operator workflow:
```sh
docker logs ai-server 2>&1 | jq 'select(.tag=="CONV_LOG_FALLBACK")'
```

---

## 8. Langfuse Cross-Reference (REQ-LOG-LANGFUSE-XREF-001)

### 8.1 `current_langfuse_trace_id() -> str | None`

`app/observability/langfuse.py`에 추가. **Non-raising**:

```python
def current_langfuse_trace_id() -> str | None:
    """Return current Langfuse v3 trace_id, or None.

    Fallback cascade (REQ-LOG-LANGFUSE-XREF-001 acceptance / m3):
      1. langfuse v3 client: get_current_observation().trace_id
      2. langfuse_context.get_current_trace_id() (contextvar accessor)
      3. None.
    Never raises — wraps every call in try/except.
    """
    if _langfuse_client is None:
        return None
    try:
        obs = _langfuse_client.get_current_observation()
        if obs is not None and obs.trace_id:
            return obs.trace_id
    except Exception:
        pass
    try:
        from langfuse import langfuse_context  # type: ignore
        tid = langfuse_context.get_current_trace_id()
        if tid:
            return tid
    except Exception:
        pass
    return None
```

**Cascade order** (resolved OQ-2): `get_current_observation` → `langfuse_context` → `None`. `RunnableConfig` metadata는 LangGraph callback handler를 통해 자동 propagate되므로 (SPEC-OBSERVABILITY-002 wiring 가정) 별도 분기 X — 만약 cascade 2개 다 None이면 그냥 None.

### 8.2 R8 — contextvar propagation across `asyncio.create_task`

**Key constraint**: `emit()`은 **caller 컨텍스트**에서 `current_langfuse_trace_id()` 호출 후 결과를 `log_event`에 인자로 전달. `asyncio.create_task` 안에서 호출하면 contextvar 손실 위험.

```python
def emit(*, ..., payload, latency_ms=None):
    # CAPTURE in caller context BEFORE spawning task
    langfuse_trace = current_langfuse_trace_id()
    truncated = _truncate(payload)
    task = asyncio.create_task(log_event(
        ..., payload=truncated, langfuse_trace=langfuse_trace, latency_ms=latency_ms,
    ))
    task.add_done_callback(_IN_FLIGHT.discard)
    _IN_FLIGHT.add(task)
```

---

## 9. TypedDict Payload Schemas — `app/observability/event_payloads.py`

19개 TypedDict, 각각 SPEC §Event Type Catalog와 1:1.

```python
# app/observability/event_payloads.py
from typing import TypedDict, NotRequired

class UserTextPayload(TypedDict):
    text: str
    lang_detected: str

class UserPhotoPayload(TypedDict):
    attachment_id: str
    image_url: NotRequired[str | None]
    caption: NotRequired[str | None]

class UserCallbackPayload(TypedDict):
    callback_data: str
    source_message_id: int

# ... (16 more)

class NodeErrorPayload(TypedDict):
    node_name: str
    exception_type: str
    message: str
    recovered: bool

__all__ = [
    "UserTextPayload", "UserPhotoPayload", "UserCallbackPayload",
    "IntentRoutedPayload", "LinkResolvedPayload", "VisionDonePayload",
    "PickItemDonePayload", "AskClarifySentPayload", "ClarifyAppliedPayload",
    "SearchDonePayload", "EvaluatorRunPayload", "DiversifyDonePayload",
    "CardSentPayload", "CardClickedPayload", "OnboardSelectPayload",
    "PinterestIngestPayload", "BotTextPayload", "TasteUpdatePayload",
    "NodeErrorPayload",
]
# CI test asserts len(__all__) == 19 — REQ-LOG-CATALOG-001 enforcement
```

`taste_update.source` 의 7 values (`click | onboard | pinterest | critique | free_text | no_click | re_query`)는 `Literal[...]` 로 강제:

```python
TasteSource = Literal["click", "onboard", "pinterest", "critique", "free_text", "no_click", "re_query"]
```

Parametric AST test (REQ-LOG-CATALOG-001 acceptance m1)가 각 source value별로 emit site 1개 이상 존재함을 검증.

---

## 10. Lifespan Wiring — `app/main.py`

기존 lifespan 안에 추가 (SPEC-MEMORY-001 startup probe 이후):

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... existing SPEC-MEMORY-001 pool init + probe ...

    # NEW — SPEC-CONVERSATION-LOG-001 reachability probe
    if MEMORY_BACKEND_IS_POSTGRES:
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1 FROM ai.log_conversation_event LIMIT 0")
            logger.info("[CONV_LOG][startup] log_conversation_event reachable")
        except Exception as exc:
            logger.warning(
                "[CONV_LOG][startup] table unreachable (%s) — emits will fallback to stderr",
                type(exc).__name__,
            )
            # Do NOT toggle MEMORY_BACKEND_IS_POSTGRES — that would disable
            # session_pg/taste_profile_pg too. Just let conversation_log
            # individual INSERTs fail and fall back to stderr per row.

    # ... existing adapter warmup ...
    yield
    # ... existing shutdown ...
```

**Why not toggle a separate `CONV_LOG_BACKEND_IS_POSTGRES` flag?** SPEC §Open Questions와 §Environment Variables 모두 새 flag 도입 X 명시. 단일 probe 실패 시 lifespan은 죽지 않고 (R3: best-effort), 매 INSERT가 stderr fallback에 들어가도록 둠. Disk noise는 수용 (operator는 startup WARN 으로 인지).

---

## 11. Test Strategy

### 11.1 Test files (11개)

| 파일 | 책임 | 의존 |
|---|---|---|
| `tests/test_conversation_log/test_emit_basic.py` | `emit()` + `log_event()` happy path + in-memory skip + WeakSet retention | testcontainers PG |
| `tests/test_conversation_log/test_thread_callback.py` | REQ-LOG-THREAD-CALLBACK-001 — 30-day window propagation + fallback + cross-user isolation + EXPLAIN | testcontainers PG |
| `tests/test_conversation_log/test_thread_propagation.py` | REQ-LOG-THREAD-001 — fresh seed, full-turn integration, multi-webhook independence | testcontainers PG |
| `tests/test_conversation_log/test_payload_cap.py` | REQ-LOG-PAYLOAD-CAP-001 — 5 field types × cap behaviors | (no DB) |
| `tests/test_conversation_log/test_payload_shapes.py` | REQ-LOG-CATALOG-001 — 19 TypedDicts smoke + `taste_update.source` parametric AST | (no DB) |
| `tests/test_conversation_log/test_search_payload.py` | REQ-LOG-PAYLOAD-RICH-001 — parallel array length + empty case + mismatched defensive | testcontainers PG |
| `tests/test_conversation_log/test_node_error.py` | REQ-LOG-EMIT-EVERY-NODE-001 — 12개 노드 강제 raise → `node_error` row + recovered flag | testcontainers PG |
| `tests/test_conversation_log/test_failsoft.py` | REQ-LOG-FAILSOFT-001 — pool patch, 1000-call concurrent property, stderr JSON parseable | testcontainers PG + capfd |
| `tests/test_conversation_log/test_langfuse_xref.py` | REQ-LOG-LANGFUSE-XREF-001 — mock v3 active/inactive + caller-context capture across task | testcontainers PG |
| `tests/test_conversation_log/test_implicit_fb_coexist.py` | REQ-LOG-IMPLICIT-FB-COEXIST-001 — 3-card + click → 4/3 row split | testcontainers PG |
| `tests/test_conversation_log/test_privacy_delete.py` + `test_gin_index.py` + `test_migration.py` | REQ-LOG-PRIVACY-001 / GIN scan EXPLAIN / migration up/down | testcontainers PG |
| `tests/test_conversation_log/test_19_event_types_smoke.py` | 100-turn synthetic load → ≥ 800 rows (DoD check) | testcontainers PG |

### 11.2 Characterization tests (DDD PRESERVE)

각 노드 수정 PR(LOG-T11~T22)는 **선행 characterization test**가 있어야 한다:

```
tests/test_graph_nodes/test_<node>_characterization.py
  - 노드 수정 전: 현재 success-path 출력 snapshot 캡처
  - 노드 수정 후: 동일 input → 동일 output (emit은 부가 효과, output 무영향)
```

이미 SPEC-AGENT-001 acceptance에 노드별 unit test 존재 가정 (CLAUDE.md). 그 test suite를 `MEMORY_FALLBACK_ON_PROBE_FAIL=true` 환경에서 한 번 실행해서 baseline 확보 → emit 추가 → 동일 결과 확인.

### 11.3 Coverage target

`app/observability/conversation_log.py` ≥ 85% (DoD). `app/observability/event_payloads.py` ≥ 95% (대부분 type def).

### 11.4 Skip conditions

testcontainers-postgres 사용 불가 환경에서는 PG-dependent tests를 `pytest.mark.skipif(...)` skip. CI는 always-on.

---

## 12. Risk Mitigation Strategies (Plan-specific)

| Risk | Plan response |
|---|---|
| R7 — task GC loss | `_IN_FLIGHT: WeakSet[asyncio.Task]` 모듈 전역. `task.add_done_callback(_IN_FLIGHT.discard)`. Stress test 10K tasks. |
| R8 — Langfuse contextvar across task boundary | §8.2 — caller context에서 capture. |
| R12 — 12 node 수정 risk | DDD PRESERVE — characterization test 선행. emit은 node body 끝에만 추가, 본문 무변경. PR review에서 diff 강제 (한 줄 try wrapper + 한 호출). |
| R13 — embedding hash | sha256-prefix-16 (`hashlib.sha256(bytes).hexdigest()[:16]`). 16 hex chars = 64-bit collision resistance ≈ 2^32 collision threshold — sufficient. |
| R14 — TypedDict drift | AST test in `test_payload_shapes.py`. CI 강제. |
| R16 — callback lookup latency | §5 — index path 보장. p99 > 50ms 측정 시 Redis cache 추가 (별도 SPEC). |

---

## 13. Approval Points (before LOG-T01 start)

1. **Migration number `0003` 확인** — branch start 시점에 `ls migrations/versions/` 재확인.
2. **`_to_jsonable` 헬퍼 추출 위치** — `app/channels/_jsonable.py` (plan §1.3 결정). SPEC-MEMORY-001 모듈 수정 발생 — 단순 import 경로 변경, 동작 무변경. PR-001 분리 가능.
3. **`card_sent.payload`에 `source_message_id` 추가** (§4.1) — SPEC catalog open-endedness 활용. v=1 유지. SPEC frozen이지만 catalog 진화는 SPEC § "Catalog evolution" 명시.
4. **OQ-8 `payload.v` 필드** — 현재 묵시 v=1. 본 plan은 **모든 payload 첫 키로 `"v": 1` 박지 않기로 결정** (현재 묵시 유지, future schema bump 시점에 retroactive add). Reason: 19개 emit site 추가 noise vs marginal benefit. 명시 도입은 SPEC 별도 v0.3.x 또는 향후 SPEC에서.

---

## 14. Out of scope (per SPEC § Non-Goals 재확인)

- 새 graph node 추가 X (Non-Goal #16).
- 검색 ranking 변경 X (Non-Goal #17).
- Critique loop iteration 변경 X (Non-Goal #18).
- Implicit feedback weight 변경 X (Non-Goal #19).
- `card_impression` schema 변경 X (Non-Goal #15).
- `CONV_LOG_ENABLED` master flag X (Non-Goal #21).
- 새 env var X (SPEC § Environment Variables).

---

End of plan.md.
