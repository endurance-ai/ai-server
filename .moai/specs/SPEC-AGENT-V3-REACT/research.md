# SPEC-AGENT-V3-REACT — Research (codebase ground truth)

> Plan phase 산출물. 모든 코드 경로·라인은 2026-05-16 시점 `feature/SPEC-AGENT-V2-REACT` 브랜치 기준 직접 검증.
> V3 는 SPEC-AGENT-V2-REACT 의 **증분 강화** — 새 그래프 토폴로지 없음. 4개 갭 각각 독립 sub-flag.

---

## 0. 검증 방법

다음 파일을 직접 Read 하여 정확한 시그니처·주입 지점·암묵 계약을 확인했다. 추정 없음.

| 파일 | 확인한 계약 |
|---|---|
| `.moai/specs/SPEC-AGENT-V2-REACT/spec.md` | REQ 네이밍(REQ-AGENT-*), OQ-7 원문, REQ-AGENT-PERF-HAPPY-001(<8s)/EXHAUST-001(<12s)/TURN-BUDGET-001(32K), flag 전략, Non-Goals 21개 |
| `app/agents/react_loop.py` (620 LOC) | 루프 구조, `_SYSTEM_PROMPT` 모듈 상수, `_build_user_message`, token budget guard 위치, terminate 조건, infinite-loop guard |
| `app/agents/tool_registry.py` (367 LOC) | 7-tool REGISTRY, `validate_args`, `ToolMetadata` TypedDict, `terminates_loop` |
| `app/agents/tools/*.py` (7개) | 각 dispatch 시그니처 `dispatch(args, ctx) -> *Result` |
| `app/graphs/nodes/agent.py` (51 LOC) | state delta 반환 형태, `run_react_loop` 래핑 |
| `app/graphs/nodes/evaluator.py` (302 LOC) | `_call_llm`/`_build_fastpath_delta`/`_fail_open_score`, `CritiqueScore`, `SELF_CRITIQUE_*` env |
| `app/channels/taste_profile.py` (258 LOC) | `TasteProfile` dataclass, `TasteProfileStore` Protocol, `reinforce_disliked_*`, `seed_from_onboarding` |
| `app/observability/conversation_log.py` + `event_payloads.py` | `emit()`, `tool_call`(20th) 이미 존재, `_summarize_payload` |
| `app/graphs/fashion_bot.py` (399 LOC) | `build_graph()` flag 분기, `_build_graph_v2()` |
| `app/core/config.py` (286 LOC) | `Settings` pydantic-settings, `AGENT_V2_*` 패턴 |
| `app/graphs/state.py` (146 LOC) | `WorkingState` 필드, V2 3필드(`agent_iterations`/`tool_call_history`/`agent_status`) |
| `app/channels/session.py` | `Session` 필드(`last_results`/`boost_keywords`/`user_intent`/`onboarded_at`/`lang`) |

---

## 1. VERIFIED CURRENT STATE (ground truth 확정)

### (a) 자율 멀티스텝 도구 오케스트레이션 — ALREADY DONE ✅ (V3 무변경)

`react_loop.py::run_react_loop(state, sess)` (line 275-619):

- `for it in range(1, max_iter + 1)` — iteration cap `settings.AGENT_MAX_ITERATIONS`(=6) (line 316)
- LLM-driven tool dispatch: `llm.ainvoke(messages)` → `ai_msg.tool_calls[0]` → `validate_args` → `_resolve_dispatcher(tool_name)` (line 445, 523)
- token budget guard: `if token_budget and cumulative_tokens >= token_budget` (line 320), `cumulative_tokens += um.get("total_tokens")` (line 382)
- infinite-loop guard: 직전 2개 history + 현재 = 3 consecutive identical → `_fallback_respond("infinite_loop_guard")` (line 482-496)
- JSON malformation: `json_malform_streak >= 2 → exhaustion` (line 391, 426)
- per-LLM/per-tool timeout: `asyncio.wait_for(..., timeout=llm_timeout/effective_timeout)` (line 339, 524)
- transient retry: `AGENT_LLM_MAX_RETRIES`(=2)/`AGENT_TOOL_MAX_RETRIES`(=1), `respond` 는 never-retry (line 515-517)
- terminate: `if REGISTRY[tool_name]["terminates_loop"]: status="done"; break` (line 588)

**결론: V3 는 이 루프 메커니즘을 건드리지 않는다. 4개 갭은 모두 (1) system 컨텍스트 조립 지점, (2) 기존 헬퍼 래핑, (3) 새 tool registry entry, (4) TasteProfile store 확장으로 한정.**

### (b) 장기 기억·취향 학습 — PARTIAL ⚠️ → Gap 1 + Gap 4

- `update_taste` tool (`app/agents/tools/update_taste.py`): `dispatch` → `get_taste_store().get_or_create(user_key)` → `reinforce_liked/disliked_*` → `store.update()`. **LLM 이 명시 호출해야만 동작.**
- `get_recent_history` tool (`app/agents/tools/get_recent_history.py`): `ai.log_conversation_event` SELECT + `_summarize_payload`. **LLM 이 명시 호출해야만 동작. in_memory backend → 빈 list fail-soft.**
- **확정 갭**: `react_loop.py::messages` (line 305-308) 는 `[{system: _SYSTEM_PROMPT}, {user: _build_user_message(...)}]` 만. TasteProfile / 최근 N턴이 매 루프 system 컨텍스트에 **자동 주입되지 않음**.

### (c) 능동적 clarify·제안 — PARTIAL ⚠️ → Gap 3

- `ask_user_clarification` tool (`app/agents/tools/ask_user_clarification.py`): `dispatch` → `clarify:{axis}:{value}` 인라인 키보드. functional 하나 LLM 판단에만 의존.
- `_SYSTEM_PROMPT` (react_loop.py line 42-72): "Use the minimum number of tool calls" — **능동 제안/선제 clarify 유도 문구 없음**. 결과 약할 때 follow-up 카드 없음. `suggest_next_step` 같은 tool 부재.

### (d) 자가평가·재시도(Reflexion) — NOT IN V2 ❌ → Gap 2 (OQ-7 resolution)

- SPEC-AGENT-V2-REACT OQ-7 원문(spec.md line 1152): *"`refine_search` tool: internal evaluator (option α) vs separate graph node (option β). plan.md decides ... Preference: fold (cleaner architecture)."*
- `app/agents/tools/refine_search.py`: line 86 `_ = action, max_price, min_price # informational only in α` — **evaluator 를 내부 호출하지 않음. auto-eval 없음.** LLM 이 자발적으로 `refine_search` 호출할 때만 동작.
- `evaluator.py` 는 모듈 docstring(line 1) `DEPRECATED — superseded by SPEC-AGENT-V2-REACT (agent loop folds evaluator into refine_search, OQ-7 α). Retained for V2.0 rollback safety only.` — **V2 agent 경로에 배선 안 됨** (`_build_graph_v2()` 가 evaluator 노드 미등록, fashion_bot.py line 176-306 확인).

**V3 Gap 2 = OQ-7 을 "evaluator 헬퍼를 루프 안에서 래핑"으로 확정 (β 변형: 노드가 아니라 in-loop helper).**

---

## 2. 갭별 정확한 주입/배선 지점 + 기존 헬퍼 시그니처

### Gap 1 — 메모리 자동 주입 (`AGENT_V3_MEMORY_INJECTION_ENABLED`)

**주입 지점**: `react_loop.py` line 305-308 의 `messages` 초기화.

```python
# 현재 (V2)
messages: list[dict[str, Any]] = [
    {"role": "system", "content": _SYSTEM_PROMPT},
    {"role": "user", "content": _build_user_message(state, sess)},
]
```

V3: flag ON 시 system 메시지를 `_SYSTEM_PROMPT + "\n\n" + _build_memory_context(...)` 로 확장. flag OFF 시 **현재와 byte-identical**.

**기존 헬퍼 시그니처 (래핑 대상, 재구현 금지)**:

- `app.channels.taste_profile.get_taste_store() -> TasteProfileStore`
  - `store.get_or_create(user_key) -> TasteProfile`
  - `TasteProfile.boost_brands(top_n=5) -> list[str]` / `boost_keywords(top_n=5) -> list[str]` / `exclude_brands(threshold=1.5) -> list[str]`
  - `TasteProfile.disliked_keywords: dict[str, float]` (직접 read OK)
- `app.agents.tools.get_recent_history.dispatch(args, ctx) -> GetRecentHistoryResult` (`{ok, error, events: list[{event_type, payload_summary}]}`) — **그대로 호출**해 최근 N턴 요약 획득. 또는 동일 SELECT + `_summarize_payload` 직접 사용.
- `user_key` 는 `react_loop.py::_build_ctx` line 165 `user_key_for(state.from_user_id, state.chat_id)` 으로 이미 ctx 에 있음.
- `session_lang(sess)` (react_loop.py line 33 import) — KO/EN 분기.

**Token budget 가드**: 주입은 `cumulative_tokens` 누적 전에 일어남(첫 messages 구성). 주입 페이로드는 별도 상한 `AGENT_V3_MEMORY_MAX_TOKENS`(신규 env, 기본 1500) + truncation. `conversation_log._truncate` 와 동일 정책(2048 chars/50 items) 재사용 가능하나 system 주입은 char 기반 cap 으로 충분.

**보안**: 주입되는 TasteProfile/history 는 시스템 파생이지만 `get_recent_history` payload 에 `user_text`/`bot_text` 가 들어감 → `_summarize_payload` 가 이미 200자 cap. 추가로 주입 블록 전체를 `[MEMORY CONTEXT — SYSTEM DERIVED]` 펜스로 감싸고, 그 안의 user-origin 텍스트는 `_summarize_payload` 의 기존 cap 에 의존(별도 `[USER INPUT — DATA ONLY]` 펜스 아님 — 이건 system role 이므로 prompt-injection 표면이 아니나, payload truncation 은 그대로 적용).

### Gap 2 — Reflexion 루프 (`AGENT_V3_REFLEXION_ENABLED`)

**배선 지점**: `react_loop.py` line 556-585, `search_products`/`refine_search` dispatch 직후 (history append + emit 사이 또는 직후).

```python
# line 556 근처
latency_ms = int((time.monotonic() - t0) * 1000)
history_entry = {... "result_summary": {...}}
history.append(history_entry)
emit(event_type="tool_call", ...)
# ── V3 Gap 2 주입 지점: tool_name in {search_products, refine_search} 이고 flag ON 이면
#    evaluator 헬퍼로 결과 평가 → quality delta 를 다음 LLM turn 의 ToolMessage 에 첨부 ──
if REGISTRY[tool_name]["terminates_loop"]: ...
messages.append(ai_msg)
messages.append(ToolMessage(content=json.dumps(result, default=str)[:2000], tool_call_id=tc_id))
```

V3: flag ON + `tool_name in ("search_products","refine_search")` + result.ok 시 → `_evaluate_search_quality(...)` 호출 → `CritiqueScore` 획득 → `ToolMessage` content 에 `"_quality": {score, retry_suggested, reason}` 머지. **agent LLM 이 그 score 를 보고 다음 iteration 에서 `refine_search` 자율 호출 OR `respond`.** 자동 강제 refine 아님 — LLM 의 결정 (더 agentic).

**기존 헬퍼 (래핑 대상, 재구현 절대 금지)**:

- `app.graphs.nodes.evaluator._call_llm(prompt_user: str) -> CritiqueScore` (async; fail-open on any error → `CritiqueScore(score=1.0, retry=False)`)
- `app.graphs.nodes._evaluator_prompt.SYSTEM_PROMPT` + `build_user_prompt(vision_item, user_intent, candidates) -> str`
- `app.graphs.nodes.evaluator._build_fastpath_delta() -> CritiqueDelta` (LLM-free broaden, 빈 결과용)
- `app.graphs.nodes._evaluator_models.CritiqueScore` (`.score: float`, `.retry: bool`, `.suggested_delta: CritiqueDelta | None`, `.reasoning: str`)
- env: `SELF_CRITIQUE_MAX_ITERATIONS`(=2), `SELF_CRITIQUE_THRESHOLD`(=0.6), `SELF_CRITIQUE_TIMEOUT_S`(=30), `EVALUATOR_MODEL/MAX_TOKENS/TEMPERATURE/TIMEOUT_S` — **그대로 재사용**.

**재시도 상한 충돌 회피**: 기존 `SELF_CRITIQUE_MAX_ITERATIONS`(=2) 재사용 — V3 는 turn 당 evaluator 호출을 이 값으로 cap (ctx 카운터 `_v3_reflexion_count`). react_loop 의 iteration cap(6) + infinite-loop guard(3 consecutive identical) 는 그대로 — Reflexion 은 그 안에 종속. evaluator 호출 자체가 추가 iteration 을 소비하지 않음(tool dispatch 내부 부가 호출). evaluator timeout 은 `EVALUATOR_TIMEOUT_S`(=8) 로 per-tool timeout(5s)과 별개 — 단, turn deadline(`turn_deadline`, line 299) 은 그대로 적용해 초과 금지.

**암묵 계약 / 위험**: `_call_llm` 은 vision_item / user_intent / candidates 를 받는 `build_user_prompt` 가 필요. V2 agent state 에는 `sess.user_intent` 가 있으나 `vision_item` 은 `state.vision_selected_item or sess.vision_item` (evaluator.py line 173). search result candidates 는 `sess.last_results` (`persist_last_results` 가 채움, search_products.py line 99-132)에서 가져와야 함 — react_loop 의 `result.top_candidates`(5개 요약)는 evaluator 입력으로 빈약. **plan.md 결정 필요**: evaluator 입력 candidates 를 `sess.last_results` 풀세트로 줄지(정확) vs top 5 만(빠름).

### Gap 3 — 능동 제안 (`AGENT_V3_PROACTIVE_ENABLED`)

**배선 지점 (둘)**:

1. `_SYSTEM_PROMPT` 강화 (react_loop.py line 42-72): flag ON 시 system prompt 에 능동성 지침 추가 — "결과가 약하거나(candidates_count < N) 모호도가 낮을 때 `suggest_next_step` 또는 `ask_user_clarification` 을 선제 호출하라". flag OFF 시 `_SYSTEM_PROMPT` byte-identical.
2. 신규 tool `suggest_next_step` — `tool_registry.py::REGISTRY` 에 entry 추가 (8번째 tool). `terminates_loop=False`. 유사 아이템 / 핏 변경 / 다른 무드 옵션 카드 발송.

**신규 tool 추가 패턴 (검증된 확장점)**:

`tool_registry.py` REGISTRY dict 에 entry 1개 + `*Args`/`*Result` TypedDict + `app/agents/tools/suggest_next_step.py::dispatch(args, ctx) -> SuggestNextStepResult`. `dispatch_fn_path` = `"app.agents.tools.suggest_next_step:dispatch"`. `_resolve_dispatcher` (react_loop.py line 128) 가 자동 lazy-import. `validate_args` (tool_registry.py line 308) 가 `__annotations__` 기반 자동 검증. **그래프 토폴로지 변경 0.** 단, flag OFF 시 REGISTRY 에서 제외돼 V2 와 7-tool 동일해야 함 → REGISTRY 를 flag-aware 하게(또는 system prompt 에서만 제외하고 tool 은 존재하되 prompt 가 호출 안 하도록 — plan.md 결정. 권장: REGISTRY 빌드 시점 flag 분기로 8번째 entry 조건부 등록 → byte-identical 보장이 명확).

**기존 헬퍼 (래핑)**: `ask_user_clarification.py::dispatch` 의 카드 빌드 로직 (인라인 키보드 `clarify:{axis}:{value}`) 재사용. suggest_next_step 은 `app.channels.clarify` / `onboarding_cards` 빌더 패턴 또는 `adapter.send_text_with_buttons` 직접. **새 카드 렌더링 알고리즘 신규 작성 금지** — 기존 `_adapter_ctx.get_adapter()` + `send_text_with_buttons` 재사용.

### Gap 4 — 크로스스레드 dislike 메모리 (`AGENT_V3_DISLIKE_MEMORY_ENABLED`)

**스키마 확장 지점**: `app/channels/taste_profile.py::TasteProfile` dataclass (line 33-107).

현재: `disliked_brands: dict[str, float]`, `disliked_keywords: dict[str, float]` — **score 만, timestamp 없음.** `reinforce_disliked_brand/keywords` 가 score 누적 + decay(0.9).

V3 확장 (additive, SPEC-MEMORY-001 Protocol 호환): timestamp 추적 추가. 옵션 A — 병렬 dict `disliked_brands_ts: dict[str, float]` (필드 추가). 옵션 B — score dict 를 `dict[str, tuple[float, float]]` 로 변경(파괴적, 기각). **plan.md 권장 = 옵션 A** (additive, dataclass default `field(default_factory=dict)`, 기존 직렬화 호환). `TasteProfileStore` Protocol (line 114-125) 은 무변경 — `update()` 시그니처 그대로. PG 구현 `taste_profile_pg.py` 가 새 dict 를 JSON 직렬화에 포함.

**자동 디스카운트 배선**: 추천 시 dislike 적용 지점 두 곳 후보:
- `TasteProfile.exclude_brands(threshold=1.5) -> list[str]` (line 93) 이미 존재 — Gap 4 는 timestamp 기반 recency 가중(최근 dislike 일수록 강하게 discount)을 이 메서드 또는 신규 메서드에 추가.
- 실제 검색 적용은 `search_products.py` / `refine_search.py` 의 `exclude_keywords` 경로 + `run_text_only_search` 의 `AnalyzedItem`. **재구현 금지** — 기존 `reinforce_disliked_*` 호출 시 timestamp 도 같이 기록하고, search dispatch 가 store 에서 dislike 를 읽어 `exclude_keywords` 에 머지하는 thin wiring 만.

**저장 트리거**: `update_taste` tool dispatch (update_taste.py line 38-45) 가 이미 `reinforce_disliked_*` 호출 → V3 는 그 호출에 timestamp 기록을 더하고, search dispatch 가 자동으로 store dislike 를 읽어 discount. 사용자가 거부 표현("이 브랜드 싫어") → LLM 이 `update_taste(brand_dislikes=[...])` 호출 → timestamp 와 함께 영구 저장 → 이후 새 thread 검색에서 자동 디스카운트.

---

## 3. 위험 · 암묵 계약 요약

| # | 위험 / 암묵계약 | 영향 | 완화 |
|---|---|---|---|
| RC1 | Gap 1 주입이 token budget 초과 → exhaustion 조기 트리거 | 중 | `AGENT_V3_MEMORY_MAX_TOKENS` cap + truncation; budget guard 는 누적 전 1회만 주입이라 첫 LLM call 만 영향 |
| RC2 | Gap 2 evaluator `_call_llm` 입력에 vision_item/candidates 풀세트 필요 — V2 loop 은 `sess.last_results` 에만 풀세트 보유 | 중 | plan.md 가 `sess.last_results` 풀세트 사용 결정; evaluator fail-open(score=1.0) 이 안전망 |
| RC3 | Gap 2 evaluator timeout(8s) > per-tool timeout(5s); turn_deadline 초과 위험 | 중 | turn_deadline guard(react_loop.py line 299) 그대로 적용; Reflexion 호출 전 deadline 체크 |
| RC4 | Gap 3 신규 tool 이 REGISTRY 7→8 → flag OFF byte-identical 깨짐 위험 | 고 | REGISTRY 빌드 시점 flag 분기 — flag OFF 시 8번째 entry 미등록 + `_SYSTEM_PROMPT` 무변경. test_byte_identical 로 검증 |
| RC5 | Gap 4 TasteProfile 직렬화 호환 — PG store 가 새 dict 미인식 시 read 실패 | 고 | additive dict (default_factory), PG 구현 `_jsonable` cascade 통과 확인; 구 row 는 새 dict 빈 채로 역직렬화 |
| RC6 | 4 flag 조합 폭발 (2^4=16) — 회귀 표면 | 중 | all-off = V2 byte-identical 단일 가드 + 각 flag 독립 ON 시나리오만 인수 (조합 전수 아님) |
| RC7 | `_SYSTEM_PROMPT` 가 모듈 상수 — flag 별 동적 조립 시 race 없음(매 turn `run_react_loop` 내 local messages 구성) | 저 | 확인 완료: messages 는 line 305 함수 local, 모듈 상수는 read-only 참조 |
| RC8 | SPEC-AGENT-V2-CLEANUP-001 "Env vars to deprecate" = `SELF_CRITIQUE_*` family 전체 + `AGENT_V2_REACT_ENABLED` + `evaluator.py` 삭제 대상 (직접 확인: cleanup spec.md "evaluator removed") | 고 | Gap 2 가 (1) evaluator 평가 헬퍼 모듈 (2) **`SELF_CRITIQUE_*` env family 전체** (3) **`EVALUATOR_*` env family 전체** 를 live dependency 화 → cleanup SPEC 충돌. **spec.md + plan.md + spec-compact.md 가 명시: cleanup followup 조정 계약은 이 3가지를 모두 보존해야 함 (단순 헬퍼 보존 불충분). `AGENT_V2_REACT_ENABLED` 무조건화는 master→sub-flag 게이트에 무해. cross-SPEC followup, non-blocking-for-run** |
| RC9 | Gap 1 `get_recent_history` in_memory backend → 빈 list. 주입이 무의미해질 수 있음 | 저 | fail-soft 그대로; TasteProfile 은 in_memory 에서도 동작하므로 최소 취향 주입은 보장 |

---

## 4. SPEC-AGENT-V2-REACT 와의 관계 (확정)

- **Increment, NOT supersede.** V2 spec.md 의 모든 REQ 유효. V3 는 V2 위에 4개 REQ 그룹 추가.
- **OQ-7 resolution**: V2 spec.md OQ-7 을 V3 가 "evaluator 헬퍼를 in-loop wrapping (β의 헬퍼 변형, 노드 아님)" 으로 **종결**. V3 spec.md 에 명시.
- **성능예산 계승**: REQ-AGENT-PERF-HAPPY-001(<8s p95), -EXHAUST-001(<12s p95), -TURN-BUDGET-001(32K) — V3 가 초과 금지. V3 REQ 가 이를 참조.
- **flag 계층**: `AGENT_V2_REACT_ENABLED`(+`AGENT_LLM_MODEL`) 가 마스터. 4개 V3 sub-flag 는 마스터 ON 일 때만 효과 (마스터 OFF 면 V1 토폴로지라 무관). 4 sub-flag all-off = V2 byte-identical.
- **cross-SPEC**: SPEC-AGENT-V2-CLEANUP-001(evaluator V2.1 제거 예정) 와 RC8 충돌 — V3 가 evaluator 헬퍼를 live 화 → cleanup SPEC 조정 필요 (followup, non-blocking for V3 plan-audit).

---

## 5. 미해결 (plan.md / Run 에서 결정)

- OQ-V3-1: Gap 2 evaluator 입력 candidates = `sess.last_results` 풀세트(정확) vs `result.top_candidates` 5개(빠름). 권장: 풀세트(정확도 우선, evaluator fail-open 이 안전망).
- OQ-V3-2: Gap 3 신규 tool 이름 — `suggest_next_step` vs `propose_followup`. 권장: `suggest_next_step` (mission 명세 따름).
- OQ-V3-3: Gap 4 timestamp 저장 입도 — per-brand/keyword 단일 last-ts vs 이력 list. 권장: 단일 last-ts (storage 최소, recency discount 에 충분).
- OQ-V3-4: Gap 1 최근 N턴 N 기본값 — 5 vs 8. 권장: 5 (token budget 보수적).
- OQ-V3-5: Gap 3 능동 제안 트리거 임계 `candidates_count < N` 의 N. 권장: 3 (V2 send 경험치).
