---
id: SPEC-CONVERSATION-LOG-001
tasks_version: 0.1.0
spec_version: 0.2.2
plan_version: 0.1.0
created: 2026-05-14
methodology: DDD (ANALYZE-PRESERVE-IMPROVE)
total_tasks: 22
---

# Task Decomposition — SPEC-CONVERSATION-LOG-001

> **Methodology**: DDD (ANALYZE-PRESERVE-IMPROVE). 12개 graph 노드는 *기존 동작* 표면이라 PRESERVE 단계(characterization test 선행) 필수. 새 모듈 3개 (`conversation_log.py`, `event_payloads.py`, migration 0003)는 greenfield.
>
> **Complexity legend**: S = < 1h, M = 2-4h, L = full day (~6-8h).
>
> **AC links**: 각 task의 `REQ` 컬럼은 SPEC §Requirements & Acceptance Criteria의 REQ-LOG-* ID를 참조. acceptance.md (별도)가 DoD 시나리오 a-g 매핑.

---

## Phase 1 — Foundation (no graph nodes touched)

These tasks build the new modules in isolation. No characterization tests needed (greenfield). Can run in **parallel** where dependency allows.

### LOG-T01 — Alembic migration `0003_create_log_conversation_event.py`

- **Description**: SPEC §Schema Reference를 그대로 구현. 10 컬럼 + 4 인덱스 (1 GIN with `jsonb_ops`). `upgrade()` + `downgrade()`. Idempotent (`IF NOT EXISTS`). No FK.
- **Files**:
  - CREATE: `migrations/versions/0003_create_log_conversation_event.py`
- **REQ**: REQ-LOG-MIGRATION-001
- **Complexity**: S
- **Predecessors**: (none — foundation)
- **First action**: `ls migrations/versions/` 재확인 — `0003` 미점유 검증. 점유 시 `0004`로 리넘버링 + `down_revision`.
- **Verification**: `alembic upgrade head` + `alembic downgrade -1` on local PG. `\d ai.log_conversation_event` 스냅샷.

### LOG-T02 — `app/observability/event_payloads.py` (19 TypedDicts)

- **Description**: SPEC §Event Type Catalog 1-19와 1:1 매핑되는 TypedDict 정의. `__all__` 에 19 entries. `taste_update.source` 7-value Literal. CI test (`test_payload_shapes.py`)가 `len(__all__) == 19` 검증.
- **Files**:
  - CREATE: `app/observability/event_payloads.py`
- **REQ**: REQ-LOG-CATALOG-001
- **Complexity**: M
- **Predecessors**: (none)
- **Verification**: `python -c "from app.observability.event_payloads import __all__; assert len(__all__)==19"`. `mypy app/observability/event_payloads.py` clean.

### LOG-T03 — `app/channels/_jsonable.py` (5-step cascade extraction)

- **Description**: SPEC-MEMORY-001의 `_to_jsonable` 5-step cascade를 새 module로 추출 (plan §1.3). `session_pg.py` / `taste_profile_pg.py` import 경로 업데이트 (단순 import 변경, 동작 무변경 — PRESERVE).
- **Files**:
  - CREATE: `app/channels/_jsonable.py`
  - MODIFY: `app/channels/session_pg.py` (import만)
  - MODIFY: `app/channels/taste_profile_pg.py` (import만)
- **REQ**: (resolves plan §1.3 OQ-1)
- **Complexity**: S
- **Predecessors**: (none — but should land before LOG-T04 to avoid duplication)
- **Characterization (DDD PRESERVE)**: existing `tests/test_memory_pg/test_session_store.py` + `test_taste_store.py` 그대로 통과해야 함 (regression check).

### LOG-T04 — `app/observability/conversation_log.py` core module

- **Description**: `log_event(...)` async (never raises) + `emit(...)` sync helper (`asyncio.create_task` wrap + WeakSet retention) + `_truncate(payload)` + `_stderr_fallback(...)` + `seed_thread()`. plan §1 전체.
- **Files**:
  - CREATE: `app/observability/conversation_log.py`
- **REQ**: REQ-LOG-FAILSOFT-001, REQ-LOG-FIRE-AND-FORGET-001, REQ-LOG-FALLBACK-001, REQ-LOG-PAYLOAD-CAP-001
- **Complexity**: L
- **Predecessors**: LOG-T03 (import `_jsonable.to_jsonable`)
- **Out of scope**: Graph 노드 integration (LOG-T11~T22). 본 task는 unit-testable module만.
- **Verification**: `tests/test_conversation_log/test_emit_basic.py` + `test_failsoft.py` + `test_payload_cap.py` 그린.

### LOG-T05 — WorkingState extension (`thread_id`, `turn_no`)

- **Description**: `app/graphs/state.py::InputState`에 `thread_id: UUID = Field(default_factory=uuid4)` + `turn_no: int = 0` 추가. `WorkingState`는 inherit이라 자동. plan §3.
- **Files**:
  - MODIFY: `app/graphs/state.py`
- **REQ**: REQ-LOG-THREAD-001, REQ-LOG-TURN-001
- **Complexity**: S
- **Predecessors**: (none)
- **Characterization (DDD PRESERVE)**: `tests/test_graph_state.py` 전체 통과 (Pydantic v2 `extra="forbid"` 와 호환 — 새 필드는 default 있어서 외부 호출자에 영향 없음).

### LOG-T06 — `current_langfuse_trace_id()` helper

- **Description**: `app/observability/langfuse.py`에 non-raising helper 추가. 3-step cascade (plan §8.1). v3 client → `langfuse_context` → None.
- **Files**:
  - MODIFY: `app/observability/langfuse.py`
- **REQ**: REQ-LOG-LANGFUSE-XREF-001 (helper 부분)
- **Complexity**: S
- **Predecessors**: (none)
- **Verification**: `tests/test_conversation_log/test_langfuse_xref.py` 부분 — v3 active/inactive + error path.

### LOG-T07 — Lifespan probe wiring (`app/main.py`)

- **Description**: `MEMORY_BACKEND_IS_POSTGRES=True` 시 `SELECT 1 FROM ai.log_conversation_event LIMIT 0` probe. 실패 시 WARN log (lifespan은 죽지 않음). Migration 미적용 시점 fallback 가시화. plan §10.
- **Files**:
  - MODIFY: `app/main.py` (lifespan 내부 한 블록 추가)
- **REQ**: (plan §10 — REQ에 직접 매핑 없으나 R3 / R10 mitigation)
- **Complexity**: S
- **Predecessors**: LOG-T01 (migration), LOG-T04 (conversation_log module)
- **Verification**: lifespan startup log에 `[CONV_LOG][startup]` 한 줄. Migration 미적용 시 WARN.

---

## Phase 2 — Webhook Intake + Thread Correlation (high-risk)

### LOG-T08 — Webhook intake emit + thread_id seed (non-callback)

- **Description**: `app/api/webhooks/telegram.py`에 graph invocation **직전** thread_id seed (uuid4) + 인바운드 종류별 emit (`user_text` / `user_photo`). plan §4.1.
- **Files**:
  - MODIFY: `app/api/webhooks/telegram.py`
- **REQ**: REQ-LOG-THREAD-001, REQ-LOG-EMIT-EVERY-NODE-001 (intake)
- **Complexity**: M
- **Predecessors**: LOG-T04, LOG-T05
- **Characterization (DDD PRESERVE)**: existing webhook unit tests (`tests/test_api/test_webhooks_telegram.py` 가정) green. emit은 side effect, response shape 무변경.
- **Verification**: `tests/test_conversation_log/test_thread_propagation.py` 일부.

### LOG-T09 — Callback thread_id correlation (REQ-LOG-THREAD-CALLBACK-001)

- **Description**: callback Update 시 30-day window 내 `card_sent` lookup → `thread_id` propagate. fallback: fresh uuid4. plan §4.1 + §5. **highest-risk task** — SQL performance + cross-user isolation 검증.
- **Files**:
  - MODIFY: `app/api/webhooks/telegram.py` (helper `_resolve_thread_id` 추가)
- **REQ**: REQ-LOG-THREAD-CALLBACK-001
- **Complexity**: L
- **Predecessors**: LOG-T08 (intake skeleton must exist), LOG-T17 (send_results emits source_message_id — see note below)
- **Note**: SPEC catalog의 `card_sent` payload에 `source_message_id` 추가 필요 (plan §4.1 마지막). 이는 LOG-T17에서 wiring. 단, LOG-T09는 LOG-T17과 독립 테스트 가능 (seed `card_sent` row 직접 INSERT in test fixture).
- **Verification**: `tests/test_conversation_log/test_thread_callback.py` — 30-day window propagation + stale (>30d) fallback + cross-user isolation 부정 테스트 + EXPLAIN check (indexed lookup, no seq scan) + p99 < 50ms (10M-row synthetic test).

### LOG-T10 — Webhook callback emit (`user_callback`)

- **Description**: callback Update 인바운드에 대한 `user_callback` emit (turn_no=0, thread_id는 LOG-T09 lookup 결과). plan §4.1 표.
- **Files**:
  - MODIFY: `app/api/webhooks/telegram.py`
- **REQ**: REQ-LOG-THREAD-CALLBACK-001 (emit 부분), REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: S
- **Predecessors**: LOG-T09
- **Verification**: `tests/test_conversation_log/test_thread_callback.py` — callback emit shape + thread_id 일치 확인.

---

## Phase 3 — Per-Node Emissions (12 nodes, DDD PRESERVE 적용)

각 노드 task는 **동일 패턴**:
1. (PRESERVE) baseline characterization test 통과 확인.
2. (IMPROVE) 노드 본문 끝에 `emit(...)` 추가 + try/except wrapper로 `node_error` emit.
3. (IMPROVE) characterization test 재실행 — 결과 동일.

대부분 **parallel 가능** (서로 독립 노드 파일). 단, 모두 LOG-T04 + LOG-T05 + LOG-T08 선행 필요.

### LOG-T11 — `ingest.py` → `intent_routed` emit

- **Description**: 노드 끝 (decision 계산 후) `intent_routed` emit. turn_no=1. plan §4.2.
- **Files**:
  - MODIFY: `app/graphs/nodes/ingest.py`
  - CREATE: `tests/test_graph_nodes/test_ingest_characterization.py` (DDD PRESERVE — 이미 있으면 reuse)
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: S
- **Predecessors**: LOG-T04, LOG-T05, LOG-T08

### LOG-T12 — `resolve_image.py` → `link_resolved` | `pinterest_ingest` emit

- **Description**: 분기별 emit. turn_no=2. plan §4.3.
- **Files**:
  - MODIFY: `app/graphs/nodes/resolve_image.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_resolve_image_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: M (두 분기 분리 + Pinterest mode payload 캡처)
- **Predecessors**: LOG-T04, LOG-T05

### LOG-T13 — `vision.py` → `vision_done` emit

- **Description**: v2 schema 전체 payload. turn_no=3. plan §4.4. `error` field LLM 실패 시 캡처. node_error wrapper에서 `recovered=True` 분기 (fallback to minimal schema).
- **Files**:
  - MODIFY: `app/graphs/nodes/vision.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_vision_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: M
- **Predecessors**: LOG-T04, LOG-T05

### LOG-T14 — `pick_item.py` → `pick_item_done` emit

- **Description**: turn_no=4. auto_picked 플래그 포함. plan §4.5.
- **Files**:
  - MODIFY: `app/graphs/nodes/pick_item.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_pick_item_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: S
- **Predecessors**: LOG-T04, LOG-T05

### LOG-T15 — `ask_clarify.py` + `apply_clarify.py` → emit

- **Description**: `ask_clarify` → `ask_clarify_sent` (turn_no=5). `apply_clarify` → `clarify_applied` (turn_no=1 callback turn). plan §4.6, §4.7.
- **Files**:
  - MODIFY: `app/graphs/nodes/ask_clarify.py`
  - MODIFY: `app/graphs/nodes/apply_clarify.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_clarify_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: M
- **Predecessors**: LOG-T04, LOG-T05

### LOG-T16 — `search.py` → `search_done` emit (ML-critical)

- **Description**: `top_k_product_ids[]` + `rrf_scores[]` parallel arrays. embedding sha256-prefix-16. `filter_drop_log` 캡처. **REQ-LOG-PAYLOAD-RICH-001 핵심 task**. plan §4.8.
- **Files**:
  - MODIFY: `app/graphs/nodes/search.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_search_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001, **REQ-LOG-PAYLOAD-RICH-001**
- **Complexity**: L (parallel array invariant + RPC raw response 캡처 + filter_drop_log instrumentation — 검색 노드의 internal logic을 *읽기*만 해야 R12 scope discipline 유지)
- **Predecessors**: LOG-T04, LOG-T05
- **Verification**: `tests/test_conversation_log/test_search_payload.py` — empty case + mismatched-length defensive AssertionError + JSON `->` extraction.

### LOG-T17 — `send_results.py` → `diversify_done` + `card_sent` (per card)

- **Description**: diversify 단계 직후 `diversify_done` emit (turn_no=8). 카드별 `card_sent` emit (turn_no=9, per card). **`source_message_id` 추가 캡처** (callback correlation 위해, plan §4.10). 기존 `card_impression` INSERT 무변경 (REQ-LOG-IMPLICIT-FB-COEXIST-001). plan §4.10.
- **Files**:
  - MODIFY: `app/graphs/nodes/send_results.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_send_results_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001, REQ-LOG-IMPLICIT-FB-COEXIST-001
- **Complexity**: L (per-card loop + Telegram message_id 캡처 + card_impression 독립성 검증)
- **Predecessors**: LOG-T04, LOG-T05
- **Verification**: `tests/test_conversation_log/test_implicit_fb_coexist.py` — 3-card → 3 + 3 row split.

### LOG-T18 — `evaluator.py` → `evaluator_run` (per iteration)

- **Description**: iteration loop 안에서 emit. turn_no=7 공유, `iteration_no` distinguisher. plan §4.9.
- **Files**:
  - MODIFY: `app/graphs/nodes/evaluator.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_evaluator_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001, REQ-LOG-TURN-001 (per-iteration same turn_no)
- **Complexity**: M
- **Predecessors**: LOG-T04, LOG-T05

### LOG-T19 — `respond.py` → `bot_text` (per chunk)

- **Description**: chunk loop 안에서 emit. turn_no=10. `_Flow` enum → payload.flow. plan §4.11.
- **Files**:
  - MODIFY: `app/graphs/nodes/respond.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_respond_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: M
- **Predecessors**: LOG-T04, LOG-T05

### LOG-T20 — `taste_update.py` → `taste_update` emit

- **Description**: source="free_text". plan §4.12.
- **Files**:
  - MODIFY: `app/graphs/nodes/taste_update.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_taste_update_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: S
- **Predecessors**: LOG-T04, LOG-T05

### LOG-T21 — `critique_apply.py` → `card_clicked` + `taste_update` emit (dual branch)

- **Description**: `crit:click:*` → `card_clicked` (turn_no=1, callback turn) + `taste_update` (source="click"). `crit:more/less/cheap` → `taste_update` (source="critique"). plan §4.13.
- **Files**:
  - MODIFY: `app/graphs/nodes/critique_apply.py`
  - CREATE/REUSE: `tests/test_graph_nodes/test_critique_apply_characterization.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001, REQ-LOG-IMPLICIT-FB-COEXIST-001 (card_clicked + card_impression.click_status 공존)
- **Complexity**: M
- **Predecessors**: LOG-T04, LOG-T05

### LOG-T22 — `node_error` wrapper 패턴 일괄 적용

- **Description**: 위 11 노드의 본문을 try/except로 일관 wrap (이미 LOG-T11~T21 에서 노드별로 추가했다면 verification만). 12번째 노드 (taste_update_implicit_feedback in `app/channels/implicit_feedback.py`)도 포함. plan §4.14.
- **Files**:
  - VERIFY/MODIFY: `app/graphs/nodes/*.py` (12개)
  - MODIFY: `app/channels/implicit_feedback.py` (taste_update emit sites — no_click / re_query / attribution)
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001 (`node_error` floor)
- **Complexity**: M (verify + 누락 채우기 + implicit_feedback emit sites 추가)
- **Predecessors**: LOG-T11~T21
- **Verification**: `tests/test_conversation_log/test_node_error.py` — 12 노드 × 강제 raise → `node_error` row + recovered flag.

---

## Phase 4 — Validation & Coverage

### LOG-T23 — REQ-LOG-CATALOG-001 19-event smoke + `taste_update.source` AST test

- **Description**: AST 기반 enumeration test. (a) `__all__` length 19 검증. (b) 7 `taste_update.source` value 각각 emit site ≥ 1 검증 (AST 스캔 `app/graphs/nodes/*.py` + `app/channels/implicit_feedback.py`).
- **Files**:
  - CREATE: `tests/test_conversation_log/test_payload_shapes.py`
- **REQ**: REQ-LOG-CATALOG-001
- **Complexity**: M
- **Predecessors**: LOG-T22 (all emit sites present)

### LOG-T24 — REQ-LOG-EMIT-EVERY-NODE-001 AST + 100-turn smoke

- **Description**: (a) AST: 12 노드 각각 `emit(...)` 호출 ≥ 1 검증. (b) 100-turn synthetic load → `SELECT count(*) WHERE created_at > now()-'5min'` ≥ 800.
- **Files**:
  - CREATE: `tests/test_conversation_log/test_19_event_types_smoke.py`
- **REQ**: REQ-LOG-EMIT-EVERY-NODE-001
- **Complexity**: M
- **Predecessors**: LOG-T22

### LOG-T25 — REQ-LOG-PRIVACY-001 + REQ-LOG-RETENTION-001 + GIN test

- **Description**: (a) DELETE WHERE user_key 2-user isolation. (b) `session_pg.py` AST scan 으로 `log_conversation_event` reference 0건 확인 (no auto-cleanup). (c) EXPLAIN on `@>` 쿼리 — `Bitmap Index Scan on idx_log_conv_payload_gin` 확인.
- **Files**:
  - CREATE: `tests/test_conversation_log/test_privacy_delete.py`
  - CREATE: `tests/test_conversation_log/test_gin_index.py`
  - CREATE: `tests/test_conversation_log/test_migration.py`
- **REQ**: REQ-LOG-PRIVACY-001, REQ-LOG-RETENTION-001, REQ-LOG-MIGRATION-001
- **Complexity**: M
- **Predecessors**: LOG-T01

### LOG-T26 — DoD manual scenarios (a-g) + Coverage gate

- **Description**: SPEC §Definition of Done 의 7 manual scenarios (a-g) 를 dev Telegram bot 또는 mocked end-to-end로 검증. `pytest --cov=app.observability.conversation_log` ≥ 85%. `ruff check . && ruff format --check .` pass. `pytest -q` overall count ≥ pre-SPEC baseline.
- **Files**:
  - REVIEW: 전체.
  - UPDATE: `acceptance.md` (별도 — DoD 결과 매핑).
- **REQ**: All P0 REQs + DoD coverage row.
- **Complexity**: M (대부분 검증, 발견된 gap만 fix)
- **Predecessors**: LOG-T01 ~ LOG-T25

---

## Dependency Graph (summary)

```
LOG-T01 (migration)─────────────────┐
LOG-T02 (event_payloads)            │
LOG-T03 (_jsonable extract)──┐      │
                             ▼      ▼
                  LOG-T04 (conversation_log core)
                             │
                  LOG-T05 (state.py thread_id)
                  LOG-T06 (langfuse helper)
                             │
                             ▼
                  LOG-T07 (lifespan probe)
                             │
                             ▼
                  LOG-T08 (webhook intake)
                             │
                             ▼
                  LOG-T09 (callback correlation) ◀── LOG-T17 (card_sent.source_message_id)
                             │
                             ▼
                  LOG-T10 (user_callback emit)
                             │
                             ▼
       ┌───────────────────────────────────────┐
       │ Phase 3 — 12 nodes in parallel        │
       │ LOG-T11..T21 + T22 (node_error wrap)  │
       └───────────────────────────────────────┘
                             │
                             ▼
       ┌───────────────────────────────────────┐
       │ Phase 4 — validation                  │
       │ LOG-T23, T24, T25, T26 (final DoD)    │
       └───────────────────────────────────────┘
```

---

## Parallelization Opportunities

- **Phase 1**: LOG-T01, LOG-T02, LOG-T03, LOG-T05, LOG-T06 모두 parallel 가능 (LOG-T04 가 T03 의존, T07 이 T01+T04 의존).
- **Phase 3 (12 nodes)**: LOG-T11~T21 모두 parallel — 서로 다른 파일. LOG-T22는 sequencing 마지막에 verification.
- **Phase 4**: LOG-T23, T24, T25 parallel. T26 final.

권장 PR 분할:
- PR-001: LOG-T01 + LOG-T02 + LOG-T03 + LOG-T05 + LOG-T06 + LOG-T07 (foundation, ~2 days)
- PR-002: LOG-T04 + Phase 1 tests (`test_emit_basic`, `test_failsoft`, `test_payload_cap`) (core module, ~1 day)
- PR-003: LOG-T08 + T09 + T10 (webhook + correlation, ~2 days)
- PR-004: LOG-T11..T21 (12 nodes, ~2-3 days if parallel pairs)
- PR-005: LOG-T22 + Phase 4 (LOG-T23..T26, validation, ~1 day)

---

## Per-Task Acceptance Tracking

각 LOG-T## 가 DoD 의 어느 checkbox에 매핑되는지 `acceptance.md`에서 한 번 더 정리됨. tasks.md는 ordering + 의존성만 책임.

End of tasks.md.
