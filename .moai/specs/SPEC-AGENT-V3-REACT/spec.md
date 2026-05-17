---
id: SPEC-AGENT-V3-REACT
version: 0.2.0
status: completed
created_at: 2026-05-16
updated_at: 2026-05-17
author: hchsa77@gmail.com
priority: P1
issue_number: 21
labels: [agentic, react-loop, memory-injection, reflexion, proactive, dislike-memory, brownfield-delta, kiko-bot]
---

# SPEC-AGENT-V3-REACT: 증분 강화 — 메모리 자동 주입 · Reflexion · 능동 제안 · 크로스스레드 dislike

## HISTORY

- 2026-05-17 (v0.2.0): 구현 완료, `status: completed` 로 전환. 마이그레이션 파일명 `0005_*` (계획 `0006_*` 에서 사실 정정). evaluator fix-cycle 1 반영 (test isolation conftest 추가 + 커버리지 보강). 최종 테스트: 810 passed / 9 pre-existing failed / 117 skipped. `Implementation Notes` 섹션 추가.
- 2026-05-16 (v0.1.0): 초안 작성. 동기 — 사용자 요구 "기존 V2 ReAct 에이전트를 GPT/Claude 급으로 끌어올려라". SPEC-AGENT-V2-REACT 가 (a) 자율 멀티스텝 도구 오케스트레이션을 이미 완성(react_loop.py ~620 LOC, 7-tool, iteration/token/timeout/infinite-loop guard 전부) 했음을 코드 직접 검증으로 확인 — 따라서 V3 는 **새 그래프 토폴로지 없는 증분 강화**다. 4개 갭만 다룬다: (1) 메모리 자동 주입, (2) Reflexion 루프(SPEC-AGENT-V2-REACT OQ-7 종결), (3) 능동 제안, (4) 크로스스레드 dislike 메모리. 각 갭은 **독립 sub-flag** 로 게이트 → 단계적 롤아웃 + 즉시 롤백. 4 sub-flag all-off 시 V2 동작과 **byte-identical**. 기존 `evaluator.py` / TasteProfile store / `conversation_log` 헬퍼는 **래핑만, 재구현 금지**. 본 SPEC 은 SPEC-AGENT-V2-REACT 를 **supersede 하지 않고 increment** 하며, V2 OQ-7 을 "evaluator 헬퍼를 in-loop wrapping" 으로 resolution 한다. RC8 (SPEC-AGENT-V2-CLEANUP-001 의 evaluator V2.1 제거 계획과 충돌) 은 cross-SPEC followup 으로 표기 — V3 plan-audit 비차단. Brownfield Delta SPEC — `[DELTA]` 마커로 V2 대비 증분만 명시.

---

## Goal

SPEC-AGENT-V2-REACT 로 kiko.ai Telegram 패션 봇의 post-onboarding 라우팅이 진짜 ReAct 에이전트가 됐다. **자율 멀티스텝 도구 오케스트레이션은 끝났다** (코드 검증: `react_loop.py::run_react_loop`, 7-tool registry, 모든 안전 가드). 그러나 GPT/Claude 급 에이전트와의 격차가 4개 남았다:

1. **메모리 자동 주입 (PARTIAL)**: `update_taste`/`get_recent_history` 툴은 있으나 **LLM 이 명시 호출해야만** 취향·맥락을 인지. 매 ReAct 루프 system 컨텍스트에 TasteProfile + 최근 N턴 요약이 **자동 주입되지 않음**.
2. **Reflexion 루프 (NOT IN V2)**: SPEC-AGENT-V2-REACT **OQ-7 보류 상태**. `refine_search` 는 LLM 자발 호출 시에만 동작 (auto-eval 없음). `evaluator.py` 는 V1 토폴로지 전용 — V2 agent 경로 미배선.
3. **능동 제안 (PARTIAL)**: `ask_user_clarification` 은 functional 하나 LLM 판단에만 의존. 결과 약할 때 follow-up 카드를 **먼저 제시하는 능동성 없음**.
4. **크로스스레드 dislike 메모리 (확정 갭)**: 사용자가 거부한 브랜드·키워드가 timestamp 와 함께 장기 저장되어 이후 추천에서 자동 디스카운트되지 않음.

본 SPEC 은 정확히 이 4개 갭만 메운다. **기존 ReAct 루프 메커니즘은 건드리지 않는다.** 4개 강화는 각각 독립 sub-flag (`AGENT_V3_MEMORY_INJECTION_ENABLED` / `AGENT_V3_REFLEXION_ENABLED` / `AGENT_V3_PROACTIVE_ENABLED` / `AGENT_V3_DISLIKE_MEMORY_ENABLED`) 로 게이트된다. 4개 모두 off 면 현재 V2 동작과 **byte-identical** — 이 가드가 검증 가능한 인수기준이다.

이 SPEC 은 **WHAT/WHY** 만 정의한다. 정확한 system prompt 문구, 메모리 요약 포맷, evaluator 입력 candidates 범위, suggest_next_step 카드 레이아웃, 롤아웃 percentage 등 **HOW** 는 `plan.md` 와 Run phase 에서 결정한다.

---

## SPEC-AGENT-V2-REACT 와의 관계 (HARD)

- [HARD] **Increment, NOT supersede.** SPEC-AGENT-V2-REACT 의 모든 REQ (REQ-AGENT-LOOP-*, -TOOL-*, -TOPOLOGY-*, -FAILURE-*, -COMPAT-*, -OBS-*, -PERF-*, -SEC-*, -CONCURRENT-*) 가 그대로 유효. V3 는 그 위에 4개 REQ 그룹을 추가.
- [HARD] **OQ-7 resolution**: SPEC-AGENT-V2-REACT spec.md Open Question 7 ("`refine_search` 내부 evaluator(α) vs 별도 그래프 노드(β)") 를 본 SPEC 이 **"evaluator 헬퍼를 ReAct 루프 안에서 wrapping (β의 헬퍼 변형 — 그래프 노드 아님, in-loop helper call)"** 로 **종결**한다. `refine_search` 는 V2 의 α(evaluator 미호출) 를 유지하고, V3 Gap 2 가 search 결과 평가를 **루프 레벨 in-loop 부가 호출**로 별도 추가한다. evaluator 노드는 그래프에 배선되지 않는다 (V1 토폴로지 무변경).
- [HARD] **성능예산 계승**: REQ-AGENT-PERF-HAPPY-001 (happy-path p95 < 8s), REQ-AGENT-PERF-EXHAUST-001 (exhausted p95 < 12s), REQ-AGENT-PERF-TURN-BUDGET-001 (per-turn 32K token cap) — V3 의 어떤 강화도 이 예산을 초과 금지.
- [HARD] **flag 계층**: `AGENT_V2_REACT_ENABLED` (+ `AGENT_LLM_MODEL` 비어있지 않음) 가 마스터 게이트. 4개 V3 sub-flag 는 마스터 ON 일 때만 효과. 마스터 OFF → V1 토폴로지 → V3 sub-flag 전부 무관(no-op).
- **cross-SPEC (followup, non-blocking — classification: non-blocking-for-run 유지)**: SPEC-AGENT-V2-CLEANUP-001 spec.md 의 "Env vars to deprecate" 가 `SELF_CRITIQUE_*` family 전체 와 `AGENT_V2_REACT_ENABLED` 를 명시적으로 deprecate ("evaluator removed") 하고 `evaluator.py` 모듈 + 4 routing 함수를 삭제 대상으로 둔다. V3 Gap 2 는 `evaluator._call_llm`/`_build_fastpath_delta`/`_evaluator_prompt.build_user_prompt`/`_evaluator_models.CritiqueScore` 모듈 **와** `SELF_CRITIQUE_MAX_ITERATIONS` / `SELF_CRITIQUE_THRESHOLD` / `SELF_CRITIQUE_TIMEOUT_S` / `EVALUATOR_MODEL` / `EVALUATOR_MAX_TOKENS` / `EVALUATOR_TEMPERATURE` / `EVALUATOR_TIMEOUT_S` env family **를 live dependency 로 승격**시킨다. 따라서 SPEC-AGENT-V2-CLEANUP-001 의 followup 조정 계약은 다음을 **모두** 보존해야 한다: (1) evaluator 평가 헬퍼 모듈 (`evaluator._call_llm`/`_build_fastpath_delta`, `_evaluator_prompt`, `_evaluator_models.CritiqueScore`) — 그래프 노드 wiring 만 제거; (2) **`SELF_CRITIQUE_*` env family 전체** (`SELF_CRITIQUE_MAX_ITERATIONS`/`THRESHOLD`/`TIMEOUT_S`) — V3 Reflexion 이 재시도 상한·임계·타임아웃에 직접 사용; (3) **`EVALUATOR_*` env family 전체** (`EVALUATOR_MODEL`/`MAX_TOKENS`/`TEMPERATURE`/`TIMEOUT_S`) — `_call_llm` 이 직접 참조. 단순 "평가 헬퍼 보존" 만으로는 불충분 — env family 보존이 명시되어야 V3 Gap 2 가 깨지지 않는다. 이 조정 PR 은 V3 plan-audit 통과 후 별도 followup (본 SPEC 의 blocker 아님). **별건 — benign**: SPEC-AGENT-V2-CLEANUP-001 의 `AGENT_V2_REACT_ENABLED` 무조건화(V2 unconditional, 항상 ON) 는 V3 의 master→sub-flag 게이트 계층에 무해하다 — 마스터가 항상 ON 이어도 4개 V3 sub-flag 가 여전히 각 갭을 독립 게이트하며 all-off byte-identical 가드는 그대로 성립한다.

---

## Scope — 4 갭, 우선순위 순

| 우선순위 | 갭 | sub-flag | 핵심 |
|---|---|---|---|
| 1 | 메모리 자동 주입 | `AGENT_V3_MEMORY_INJECTION_ENABLED` | TasteProfile + 최근 N턴 요약을 매 ReAct 루프 system 컨텍스트에 암묵 주입. token budget 안에서 truncation |
| 2 | Reflexion 루프 | `AGENT_V3_REFLEXION_ENABLED` | search/refine 결과를 기존 evaluator 헬퍼로 평가 → quality delta 를 다음 LLM turn 에 첨부 → LLM 자율 refine 결정 |
| 3 | 능동 제안 | `AGENT_V3_PROACTIVE_ENABLED` | 신규 `suggest_next_step` tool + system prompt 능동성 강화 (결과 약/모호 시 선제 제안·clarify) |
| 4 | 크로스스레드 dislike | `AGENT_V3_DISLIKE_MEMORY_ENABLED` | TasteProfile 스키마 additive 확장 (dislike + timestamp) → 이후 추천 자동 디스카운트 |

---

## Architecture Snapshot (informative)

After this SPEC (V2 increment — 모든 변경은 in-loop / registry / store, 그래프 토폴로지 무변경):

```
Telegram webhook → ingest → [onboarding gate] → agent node (UNCHANGED graph node)
                                                   ↓
                                  run_react_loop(state, sess)  [react_loop.py — V2 mechanism UNCHANGED]
                                    │
                                    ├─ [DELTA Gap1] flag ON: messages[0].system =
                                    │      _SYSTEM_PROMPT + _build_memory_context(taste, recent_N)
                                    │      (token-capped, [MEMORY CONTEXT — SYSTEM DERIVED] fence)
                                    │      flag OFF: _SYSTEM_PROMPT only (byte-identical V2)
                                    │
                                    ├─ for it in 1..6:  [iteration/token/timeout/infinite-loop guards UNCHANGED]
                                    │     llm.ainvoke(messages) → tool_call → dispatch
                                    │
                                    │     [DELTA Gap2] flag ON & tool ∈ {search_products, refine_search} & ok:
                                    │         score = _evaluate_search_quality(...)  [wraps evaluator._call_llm]
                                    │         ToolMessage.content += {"_quality": {score, retry_suggested, reason}}
                                    │         (LLM sees score → autonomously decides refine_search OR respond)
                                    │         capped by SELF_CRITIQUE_MAX_ITERATIONS + turn_deadline
                                    │
                                    │     [DELTA Gap3] flag ON: REGISTRY gains 8th tool `suggest_next_step`;
                                    │         _SYSTEM_PROMPT gains proactive directive block
                                    │         flag OFF: REGISTRY = V2 7-tool, prompt byte-identical
                                    │
                                    └─ respond (terminal) — UNCHANGED

Persistence:
  [DELTA Gap4] flag ON: TasteProfile gains additive dislike-timestamp tracking;
      update_taste dispatch records ts; search_products/refine_search dispatch
      reads store dislike → auto-discount via existing exclude path.
      flag OFF: TasteProfile schema/behaviour byte-identical V2.
  tool_call event (20th, already in catalog) — unchanged.
```

**Affected modules (informational — exact in plan.md)**:

- `app/agents/react_loop.py` — MODIFIED. `messages` 초기화 지점에 Gap1 메모리 컨텍스트 조립; search/refine dispatch 직후 Gap2 평가 훅; `_SYSTEM_PROMPT` 사용 지점 flag-aware. **루프 메커니즘(iteration/token/timeout/guard) 무변경.**
- `app/agents/_memory_context.py` — NEW. `_build_memory_context(state, sess, ctx) -> str` — TasteProfile + get_recent_history 래핑, token-cap. (재구현 아님 — 기존 헬퍼 호출 + 포맷팅만.)
- `app/agents/_reflexion.py` — NEW. `evaluate_search_quality(...) -> dict` — `evaluator._call_llm` + `_build_fastpath_delta` 래핑. (재구현 아님.)
- `app/agents/tools/suggest_next_step.py` — NEW (Gap3). thin wrapper — `_adapter_ctx.get_adapter()` + `send_text_with_buttons` 재사용.
- `app/agents/tool_registry.py` — MODIFIED. flag-aware REGISTRY 빌드 — Gap3 flag ON 시 8번째 entry `suggest_next_step` 등록; OFF 시 V2 7-tool byte-identical. `SuggestNextStepArgs/Result` TypedDict 추가.
- `app/channels/taste_profile.py` — MODIFIED (Gap4). `TasteProfile` 에 additive dislike-timestamp dict 필드 + recency-aware exclude 헬퍼. `TasteProfileStore` Protocol 무변경.
- `app/channels/taste_profile_pg.py` — MODIFIED (Gap4). 새 dict 를 기존 `_jsonable` cascade 직렬화에 포함. `update()` 시그니처 무변경.
- `app/agents/tools/update_taste.py` — MODIFIED (Gap4). `reinforce_disliked_*` 호출 시 timestamp 동반 기록 (flag-gated).
- `app/agents/tools/search_products.py` / `refine_search.py` — MODIFIED (Gap4). store dislike 를 읽어 기존 `exclude_keywords` 경로에 머지 (flag-gated; 새 검색 알고리즘 아님).
- `app/core/config.py` — MODIFIED. 5 env 추가: 4 sub-flag + `AGENT_V3_MEMORY_MAX_TOKENS`. (plan.md 가 추가 튜닝 env 결정 가능.)
- `tests/test_agent_v3/` — NEW 디렉토리. 갭별 + byte-identical 회귀 + 성능 가드 + edge.

**Reused, untouched (asserted)**:

- `app/graphs/fashion_bot.py` — 토폴로지 무변경 (`_build_graph_v2()` 그대로). V3 flag 는 토폴로지에 영향 없음.
- `app/graphs/nodes/agent.py` — state delta 반환 무변경.
- `app/graphs/nodes/evaluator.py` — **헬퍼만 import 됨, 본문 무변경.** (그래프 노드 wiring 은 V1 에만, V2/V3 미배선.)
- `app/graphs/state.py` — `WorkingState` 무변경 (V2 의 3필드로 충분; Gap 들은 추가 state 필드 불필요 — ctx dict + store 로 충분).
- `app/observability/conversation_log.py` / `event_payloads.py` — `tool_call`(20th) 이미 존재, 무변경.
- `app/pipeline/**`, `app/providers/**`, `app/channels/{vision,clarify,session,lang}.py` — 래핑만.

`[DELTA]` 마커: 위 "Affected modules" 의 MODIFIED 항목이 V2 대비 brownfield 증분. NEW 는 신규 wrapper.

---

## Requirements & Acceptance Criteria

### REQ Index

| REQ-ID | Title | Priority |
|---|---|---|
| REQ-AGENT-V3-MEM-INJECT-001 | flag ON 시 TasteProfile + 최근 N턴 요약을 system 컨텍스트에 자동 주입 | P1 |
| REQ-AGENT-V3-MEM-CAP-001 | 메모리 주입 페이로드는 token cap + truncation 내로 제한 | P1 |
| REQ-AGENT-V3-MEM-FLAG-001 | `AGENT_V3_MEMORY_INJECTION_ENABLED` OFF 시 messages byte-identical V2 | P0 |
| REQ-AGENT-V3-REFLEX-EVAL-001 | flag ON 시 search/refine 결과를 기존 evaluator 헬퍼로 평가 (재구현 금지) | P1 |
| REQ-AGENT-V3-REFLEX-DELTA-001 | 평가 quality delta 를 다음 LLM turn 컨텍스트에 첨부 — LLM 자율 refine 결정 | P1 |
| REQ-AGENT-V3-REFLEX-BOUND-001 | Reflexion 호출 횟수를 SELF_CRITIQUE_MAX_ITERATIONS 로 cap, infinite-loop guard 무충돌 | P0 |
| REQ-AGENT-V3-REFLEX-DEADLINE-001 | Reflexion 은 잔여 turn budget timeout 으로 강제 wrap·취소 (overrun 기계적 차단) | P0 |
| REQ-AGENT-V3-REFLEX-FLAG-001 | `AGENT_V3_REFLEXION_ENABLED` OFF 시 루프 동작 byte-identical V2 | P0 |
| REQ-AGENT-V3-PROACT-TOOL-001 | flag ON 시 8번째 tool `suggest_next_step` 가 REGISTRY 에 등록 | P1 |
| REQ-AGENT-V3-PROACT-PROMPT-001 | flag ON 시 system prompt 에 능동성 지침 추가 (선제 제안·clarify 유도) | P1 |
| REQ-AGENT-V3-PROACT-FLAG-001 | `AGENT_V3_PROACTIVE_ENABLED` OFF 시 REGISTRY=7-tool + prompt byte-identical V2 | P0 |
| REQ-AGENT-V3-DISLIKE-SCHEMA-001 | TasteProfile 스키마 additive 확장 — dislike + timestamp (재구현 금지) | P1 |
| REQ-AGENT-V3-DISLIKE-DISCOUNT-001 | 저장된 cross-thread dislike 가 이후 검색에서 자동 디스카운트 | P1 |
| REQ-AGENT-V3-DISLIKE-FLAG-001 | `AGENT_V3_DISLIKE_MEMORY_ENABLED` OFF 시 TasteProfile schema/behaviour byte-identical V2 | P0 |
| REQ-AGENT-V3-COMPAT-ALLOFF-001 | 4 sub-flag all-off 시 전체 V2 동작 byte-identical (단일 회귀 가드) | P0 |
| REQ-AGENT-V3-COMPAT-WRAP-001 | evaluator / TasteProfile store / conv_log 헬퍼는 래핑만 — 평가/메모리 로직 신규 작성 금지 | P0 |
| REQ-AGENT-V3-PERF-001 | 4 flag ON 시에도 REQ-AGENT-PERF-HAPPY-001 (p95<8s) / -EXHAUST-001 (p95<12s) 예산 미초과 | P1 |
| REQ-AGENT-V3-SEC-001 | 메모리 주입 페이로드도 prompt-injection 격리 + payload truncation 적용 | P1 |

---

### 모듈 1 — 메모리 자동 주입 (REQ-AGENT-V3-MEM-*)

#### REQ-AGENT-V3-MEM-INJECT-001 — TasteProfile + 최근 N턴 요약 자동 주입 [P1]

**WHERE** `AGENT_V3_MEMORY_INJECTION_ENABLED=true` (그리고 마스터 `AGENT_V2_REACT_ENABLED` ON),
**THE SYSTEM SHALL** `run_react_loop` 의 system 메시지를 `_SYSTEM_PROMPT` + 메모리 컨텍스트 블록으로 구성한다. 메모리 컨텍스트는 (a) 해당 user_key 의 TasteProfile 요약 (top liked brands/keywords, disliked 요약 — 기존 `TasteProfile.boost_brands/boost_keywords/exclude_brands` 헬퍼 사용), (b) 최근 N턴 대화 요약 (기존 `get_recent_history` 툴의 dispatch 로직 또는 동일 SELECT + `_summarize_payload` 래핑) 을 포함한다. N 기본값은 **5** (OQ-V3-4 resolved — recommendation adopted). LLM 은 `get_recent_history` 툴을 명시 호출하지 않고도 취향·맥락을 인지한다.

**Acceptance**:

- flag ON + 시드된 TasteProfile(`liked_brands={"ami":2.0}`) + 3개 과거 conv event 가 있는 user 로 `run_react_loop` 실행 → 첫 LLM call 의 system 메시지에 `"ami"` 와 최근 턴 요약이 포함됨을 mock LLM 의 입력 캡처로 검증.
- flag ON + TasteProfile 빈 + in_memory backend (get_recent_history 빈 list) → 주입 블록은 빈 plceholder ("(no taste history yet)") 이고 루프는 정상 진행 (fail-soft).
- 단위 테스트: `_build_memory_context` 가 `TasteProfile.boost_brands()` / `get_recent_history` dispatch 를 호출하고 새 평가/요약 로직을 신규 구현하지 않음 (AST: 단일 import + 호출).

#### REQ-AGENT-V3-MEM-CAP-001 — 메모리 주입 token cap + truncation [P1]

**THE SYSTEM SHALL** 메모리 컨텍스트 블록의 크기를 `AGENT_V3_MEMORY_MAX_TOKENS` (기본 1500, char-근사) 이내로 제한한다. 초과 시 최근 턴부터 우선 보존하며 잘라낸다 (taste 요약 우선, 그다음 recent turns 최신순). 이 cap 은 기존 per-turn token budget (`AGENT_TURN_TOKEN_BUDGET`=32K, REQ-AGENT-PERF-TURN-BUDGET-001) 와 독립적이며 그 안에 종속된다 — 주입이 첫 LLM call 토큰을 늘려도 누적 budget guard 가 그대로 동작.

**Acceptance**:

- 50개 conv event + 50개 liked_keywords 를 시드 → 메모리 블록 char 길이가 cap*4 (≈token*4) 이내. 최신 턴이 보존되고 오래된 턴이 잘림.
- `AGENT_V3_MEMORY_MAX_TOKENS=200` env 오버라이드 → 블록이 더 작아짐.
- 주입 후에도 `run_react_loop` 의 누적-토큰 budget exhaustion guard (`cumulative_tokens >= AGENT_TURN_TOKEN_BUDGET`) 가 정상 트리거됨을 검증.

#### REQ-AGENT-V3-MEM-FLAG-001 — flag OFF → messages byte-identical V2 [P0]

**IF** `AGENT_V3_MEMORY_INJECTION_ENABLED=false`,
**THEN THE SYSTEM SHALL** `run_react_loop` 의 초기 `messages` 를 V2 와 byte-identical (`[{system: _SYSTEM_PROMPT}, {user: _build_user_message(...)}]`) 로 유지한다 — 메모리 컨텍스트 호출 자체가 일어나지 않는다.

**Acceptance**:

- flag OFF 로 `run_react_loop` 실행 → 첫 LLM call 의 messages 가 V2 baseline 스냅샷과 byte-exact 일치.
- flag OFF 시 `_build_memory_context` / `get_recent_history` (메모리 주입 경로) 가 호출되지 않음을 spy 로 검증 (불필요 DB SELECT 0건).

---

### 모듈 2 — Reflexion 루프 (REQ-AGENT-V3-REFLEX-*) — V2 OQ-7 resolution

#### REQ-AGENT-V3-REFLEX-EVAL-001 — search/refine 결과를 기존 evaluator 헬퍼로 평가 [P1]

**WHEN** `AGENT_V3_REFLEXION_ENABLED=true` 이고 ReAct 루프가 `search_products` 또는 `refine_search` tool dispatch 를 성공(`result.ok`) 으로 완료했을 때,
**THE SYSTEM SHALL** 기존 `app.graphs.nodes.evaluator._call_llm` (+ 빈 결과 시 `_build_fastpath_delta`) 를 래핑해 검색 결과 품질 `CritiqueScore` 를 산출한다. 평가 입력 candidates 는 `sess.last_results` 풀세트 (`plan.md` OQ-V3-1 결정), vision/intent 컨텍스트는 기존 evaluator 와 동일 소스. **평가 로직은 신규 작성하지 않는다 — `evaluator._call_llm`/`_build_fastpath_delta`/`_evaluator_prompt.build_user_prompt` 호출만.**

**Acceptance**:

- flag ON + search_products 가 5 candidates 반환 → `_reflexion.evaluate_search_quality` 가 `evaluator._call_llm` 을 정확히 1회 호출하고 그 `CritiqueScore` 를 반환함을 검증 (mock).
- flag ON + search 가 0 candidates → `_build_fastpath_delta` (LLM-free broaden) 경로 사용, evaluator LLM 미호출.
- AST 테스트: `_reflexion.py` 가 `evaluator` 모듈에서 `_call_llm`/`_build_fastpath_delta` 를 import 하고 자체 LLM 평가 프롬프트를 신규 정의하지 않음.
- evaluator `_call_llm` 이 timeout/error → fail-open `CritiqueScore(score=1.0, retry=False)` 가 그대로 전파됨 (재구현 없음 증거).

#### REQ-AGENT-V3-REFLEX-DELTA-001 — quality delta 를 다음 LLM turn 에 첨부, LLM 자율 결정 [P1]

**WHEN** Reflexion 평가가 `CritiqueScore` 를 산출했을 때,
**THE SYSTEM SHALL** 그 score (score 값, retry_suggested 여부, reason 요약) 를 해당 tool dispatch 의 `ToolMessage` content 에 `"_quality"` 키로 머지해 다음 LLM iteration 컨텍스트에 노출한다. agent LLM 은 이 score 를 보고 **자율적으로** 다음 행동(추가 `refine_search` 호출 OR `respond` 종결)을 결정한다 — V3 는 강제 refine 을 하지 않는다 (더 agentic; V1 evaluator 의 결정형 retry 와 대비).

**Acceptance**:

- flag ON + search 결과 score=0.3 (낮음) → 다음 LLM call 의 ToolMessage content 에 `"_quality":{"score":0.3,...}` 포함 검증. mock LLM 이 `refine_search` 호출 → 정상 dispatch.
- flag ON + score=0.9 (높음) → ToolMessage 에 score 첨부되나, mock LLM 이 `respond` 호출 → 정상 종결 (강제 refine 없음).
- flag ON + score=0.3 이어도 LLM 이 `respond` 선택 시 그대로 종결 — V3 가 LLM 결정을 오버라이드하지 않음을 검증.

#### REQ-AGENT-V3-REFLEX-BOUND-001 — Reflexion 호출 횟수 cap [P0]

**THE SYSTEM SHALL** turn 당 Reflexion(evaluator) 호출 횟수를 기존 `SELF_CRITIQUE_MAX_ITERATIONS` (기본 2) 로 cap 한다 (per-turn ctx 카운터). evaluator 호출은 ReAct 루프의 iteration counter 를 증가시키지 않으며 (tool dispatch 내부 부가 호출), 루프의 infinite-loop guard (3 consecutive identical tool call) 와 충돌하지 않는다 — Reflexion 은 `tool_call_history` append 에 영향을 주지 않는다 (평가 결과는 ToolMessage content 머지로만 노출, history entry 무변경).

**Acceptance**:

- flag ON + LLM 이 search_products 를 6회 호출 시도 → evaluator 호출은 최대 `SELF_CRITIQUE_MAX_ITERATIONS`(2) 회만 발생, 이후 search 결과에는 `_quality` 미첨부 (cap 도달).
- flag ON + LLM 이 동일 search_products(같은 args) 3회 연속 → ReAct 루프 infinite-loop guard 가 정상 트리거 (Reflexion 이 history 를 오염시키지 않음 검증 — 평가 전후 `tool_call_history` 동일).
- flag ON + evaluator 호출 후 ReAct iteration counter 가 평가로 인해 증가하지 않음 검증.

#### REQ-AGENT-V3-REFLEX-DEADLINE-001 — Reflexion 은 잔여 turn budget 으로 강제 취소 [P0]

**WHEN** `AGENT_V3_REFLEXION_ENABLED=true` 이고 ReAct 루프가 Reflexion(evaluator) 평가를 수행하려 할 때,
**THE SYSTEM SHALL** evaluator 호출을 `remaining = max(0.0, turn_deadline - now)` 의 **잔여 budget timeout** 으로 강제 wrap 한다 (`asyncio.wait_for(evaluate_search_quality(...), timeout=remaining)` 또는 동치). evaluator 가 `remaining` 초 안에 완료하지 못하면 **그 호출은 즉시 취소(cancel)되고**, 해당 search/refine dispatch 의 ToolMessage 는 `_quality` 없이 (또는 `{"_quality": {"skipped": true, "reason": "deadline"}}` 로) 진행되며, turn 전체는 `turn_deadline` 및 상속된 REQ-AGENT-PERF-HAPPY-001 (p95 < 8s) / REQ-AGENT-PERF-EXHAUST-001 (p95 < 12s) 예산을 **초과하지 않는다**. `remaining` 이 0 이면 evaluator 를 호출하지 않고 즉시 skip 한다. pre-call deadline 체크만으로는 불충분하다 — wrapped `evaluator._call_llm` 의 `EVALUATOR_TIMEOUT_S` (기본 8s) 가 잔여 budget 보다 클 수 있으므로, 취소-on-overrun 이 정규(normative) 동작이며 선택적이지 않다.

**Rationale (D2)**: pre-check 가 t≈1s 에서 turn_deadline t≈8s 로 통과해도, `EVALUATOR_TIMEOUT_S`=8s evaluator 호출이 t≈9s 에 끝나면 turn_deadline 과 p95<8s 예산을 동시에 초과한다. 잔여-budget wrap + 강제 취소가 이 overrun 을 기계적으로 차단한다.

**Acceptance**:

- flag ON + slow evaluator stub (응답까지 20s) + turn_deadline 까지 잔여 2s → evaluator 호출이 잔여-budget 경계(≈2s)에서 **취소**되고, ToolMessage 는 `_quality` skipped 로 진행, turn 이 `turn_deadline` 안에 완료됨을 검증 (mechanical: stub 주입 + 취소 시점 단언 + turn 완료 시각 ≤ turn_deadline 단언).
- flag ON + `remaining ≤ 0` → evaluator 0회 호출 (skip), 즉시 다음 단계 진행.
- flag ON + 4 flag ON + slow evaluator → 200턴 happy-path p95 < 8s, 50턴 exhausted p95 < 12s (상속 예산 미초과, REQ-AGENT-V3-PERF-001 / acceptance.md AC-P.2 와 정합).
- flag ON + fast evaluator (잔여 budget 충분) → 정상 평가 완료, `_quality` 첨부 (positive control).

#### REQ-AGENT-V3-REFLEX-FLAG-001 — flag OFF → 루프 byte-identical V2 [P0]

**IF** `AGENT_V3_REFLEXION_ENABLED=false`,
**THEN THE SYSTEM SHALL** search/refine dispatch 후 ToolMessage content 를 V2 와 byte-identical (`json.dumps(result, default=str)[:2000]` — `run_react_loop` 의 기존 ToolMessage 직렬화 형식) 로 유지하고 evaluator 헬퍼를 호출하지 않는다.

**Acceptance**:

- flag OFF + search_products dispatch → ToolMessage content 가 V2 baseline 과 byte-exact, `"_quality"` 키 부재.
- flag OFF → `evaluator._call_llm` (Reflexion 경로) 0회 호출 (spy).

---

### 모듈 3 — 능동 제안 (REQ-AGENT-V3-PROACT-*)

#### REQ-AGENT-V3-PROACT-TOOL-001 — 8번째 tool `suggest_next_step` 등록 [P1]

**WHERE** `AGENT_V3_PROACTIVE_ENABLED=true`,
**THE SYSTEM SHALL** `tool_registry.py::REGISTRY` 에 8번째 tool `suggest_next_step` 를 등록한다 (`SuggestNextStepArgs`/`SuggestNextStepResult` TypedDict, `dispatch_fn_path="app.agents.tools.suggest_next_step:dispatch"`, `terminates_loop=False`, `validate_args` 자동 적용). 이 tool 은 유사 아이템 / 핏 변경 / 다른 무드 등 후속 행동 옵션 카드를 발송하며, 기존 `_adapter_ctx.get_adapter()` + `send_text_with_buttons` 를 재사용한다 (새 카드 렌더링 알고리즘 금지).

**Acceptance**:

- flag ON → `REGISTRY` 에 `"suggest_next_step"` 존재, `TOOL_NAMES` 길이 8, `validate_args("suggest_next_step", {...})` 동작.
- flag ON + LLM 이 `suggest_next_step(options=[...])` 호출 → adapter 의 `send_text_with_buttons` 가 호출됨 (mock), 루프 미종결 (`terminates_loop=False`).
- AST 테스트: `suggest_next_step.py` 가 단일 adapter import + send 호출, 새 카드 렌더 로직 미정의 (line ≤ 80).

#### REQ-AGENT-V3-PROACT-PROMPT-001 — system prompt 능동성 지침 추가 [P1]

**WHERE** `AGENT_V3_PROACTIVE_ENABLED=true`,
**THE SYSTEM SHALL** `run_react_loop` 의 system prompt 에 능동성 지침 블록을 추가한다: (a) 검색 결과가 약할 때 (`candidates_count < N`, N = **3**, OQ-V3-5 resolved) `suggest_next_step` 으로 follow-up 을 선제 제시, (b) 사용자 의도 모호도가 낮을 때 `ask_user_clarification` 을 선제 유도. flag OFF 시 `_SYSTEM_PROMPT` 는 byte-identical V2.

**Acceptance**:

- flag ON → 첫 LLM call system 메시지에 능동성 지침 문자열 포함 (입력 캡처).
- flag ON + search 가 1 candidate (약함) → mock LLM 이 `suggest_next_step` 호출 가능 (지침이 컨텍스트에 있음 검증; LLM 결정 자체는 강제 아님).
- flag OFF → system 메시지가 V2 `_SYSTEM_PROMPT` 와 byte-exact.

#### REQ-AGENT-V3-PROACT-FLAG-001 — flag OFF → REGISTRY 7-tool + prompt byte-identical [P0]

**IF** `AGENT_V3_PROACTIVE_ENABLED=false`,
**THEN THE SYSTEM SHALL** `REGISTRY` 를 V2 의 정확히 7-tool 로 유지하고 (`suggest_next_step` 미등록, `TOOL_NAMES` 길이 7), `_SYSTEM_PROMPT` 를 V2 byte-identical 로 유지한다.

**Acceptance**:

- flag OFF → `TOOL_NAMES == ("analyze_image","search_products","refine_search","update_taste","ask_user_clarification","get_recent_history","respond")` (V2 7-tool 정확 일치).
- flag OFF → system 메시지 byte-exact V2.

---

### 모듈 4 — 크로스스레드 dislike 메모리 (REQ-AGENT-V3-DISLIKE-*)

#### REQ-AGENT-V3-DISLIKE-SCHEMA-001 — TasteProfile additive 확장 (dislike + timestamp) [P1]

**WHERE** `AGENT_V3_DISLIKE_MEMORY_ENABLED=true`,
**THE SYSTEM SHALL** `app.channels.taste_profile.TasteProfile` 를 **additive** 하게 확장해 거부된 브랜드·키워드의 last-dislike timestamp 를 추적한다 (신규 dataclass 필드, `field(default_factory=dict)` 기본값, 기존 필드·`reinforce_disliked_*` semantics·`TasteProfileStore` Protocol·`update()` 시그니처 무변경). 이는 SPEC-MEMORY-001 TasteProfile 의 **스키마 확장이며 재구현이 아니다**. `update_taste` tool dispatch 가 `reinforce_disliked_*` 호출 시 timestamp 를 동반 기록한다 (flag-gated).

**Acceptance**:

- flag ON + `update_taste(brand_dislikes=["zara"])` → TasteProfile 의 새 timestamp dict 에 `"zara": <ts>` 기록, 기존 `disliked_brands["zara"]` score 도 정상 누적.
- 기존 V2 직렬화 호환: flag ON 으로 생성된 TasteProfile 을 `taste_profile_pg` 의 기존 `_jsonable` cascade 로 직렬화 → `json.dumps` 성공. 새 timestamp dict 없는 구 row 를 역직렬화 → 새 dict 빈 채로 정상 로드 (KeyError 없음).
- snapshot 테스트: `TasteProfile` 필드 집합이 V2 superset (정확히 신규 timestamp dict 만 추가), 기존 필드 타입/기본값 무변경.

#### REQ-AGENT-V3-DISLIKE-DISCOUNT-001 — 저장된 dislike 자동 디스카운트 [P1]

**WHEN** `AGENT_V3_DISLIKE_MEMORY_ENABLED=true` 이고 `search_products`/`refine_search` tool 이 dispatch 될 때,
**THE SYSTEM SHALL** 해당 user_key 의 TasteProfile 에서 cross-thread dislike (timestamp recency 가중) 를 읽어 기존 `exclude_keywords`/`exclude_brands` 경로에 머지한다. 이전 thread 에서 거부된 브랜드·키워드가 새 thread 검색에서 자동 디스카운트된다. **새 검색 알고리즘을 작성하지 않는다 — 기존 `TasteProfile.exclude_brands` + search dispatch 의 exclude 경로 재사용.**

**Acceptance**:

- thread A 에서 `update_taste(brand_dislikes=["gucci"])` → 새 thread B 에서 `search_products(text_query="bag")` → dispatch 가 store dislike 를 읽어 exclude 에 "gucci" 머지됨 (검색 입력 캡처로 검증).
- recency 가중: 오래된 dislike 보다 최근 dislike 가 더 강하게 discount (헬퍼 단위 테스트로 가중 함수 검증).
- AST 테스트: discount 머지가 기존 `exclude_keywords` 경로를 재사용, 새 ranking/검색 로직 미정의.

#### REQ-AGENT-V3-DISLIKE-FLAG-001 — flag OFF → TasteProfile byte-identical V2 [P0]

**IF** `AGENT_V3_DISLIKE_MEMORY_ENABLED=false`,
**THEN THE SYSTEM SHALL** `TasteProfile` 의 schema 와 behaviour, `update_taste`/`search_products`/`refine_search` dispatch 의 동작을 V2 byte-identical 로 유지한다 — timestamp 기록도, dislike 디스카운트 머지도 일어나지 않는다.

**Acceptance**:

- flag OFF + `update_taste(brand_dislikes=["zara"])` → timestamp dict 빈 상태 유지 (기록 안 함), `disliked_brands` 만 V2 처럼 갱신.
- flag OFF + 시드된 cross-thread dislike → `search_products` dispatch 의 exclude 입력이 V2 baseline 과 byte-identical (디스카운트 미적용).

---

### 횡단 — 호환 · 래핑 · 성능 · 보안

#### REQ-AGENT-V3-COMPAT-ALLOFF-001 — 4 sub-flag all-off → 전체 byte-identical V2 [P0]

**IF** `AGENT_V3_MEMORY_INJECTION_ENABLED`, `AGENT_V3_REFLEXION_ENABLED`, `AGENT_V3_PROACTIVE_ENABLED`, `AGENT_V3_DISLIKE_MEMORY_ENABLED` 가 **모두 false** (마스터 `AGENT_V2_REACT_ENABLED` 는 ON),
**THEN THE SYSTEM SHALL** 전체 ReAct 루프 동작 (messages 구성, tool registry, tool dispatch 결과, TasteProfile schema/behaviour) 을 SPEC-AGENT-V2-REACT 와 **byte-identical** 로 유지한다. 이것이 V3 의 단일 회귀 가드다.

**Acceptance**:

- 4 flag all-off + 대표 시나리오 6종 (사진+검색 / 텍스트검색 / 더저렴 / 취향업데이트 / off-topic / clarify) → 각 시나리오의 LLM 입력 messages, REGISTRY, ToolMessage content 가 V2 baseline 과 byte-exact.
- 4 flag all-off 로 기존 `tests/test_agent_v2/` 전 스위트 무변경 통과.
- `TOOL_NAMES` 길이 7, system 메시지 = V2 `_SYSTEM_PROMPT`, TasteProfile 필드집합 = V2.

#### REQ-AGENT-V3-COMPAT-WRAP-001 — 헬퍼 래핑만, 신규 평가/메모리 로직 금지 [P0]

**THE SYSTEM SHALL** Gap1 메모리 주입은 기존 `get_recent_history` dispatch / `TasteProfile.boost_*` 헬퍼를, Gap2 Reflexion 은 기존 `evaluator._call_llm`/`_build_fastpath_delta`/`_evaluator_prompt.build_user_prompt` 를, Gap4 dislike 는 기존 `TasteProfile.reinforce_disliked_*`/`exclude_brands` + `_jsonable` cascade 를 **래핑만** 한다. 새 LLM 평가 프롬프트, 새 메모리 요약 알고리즘, 새 검색/ranking 로직을 신규 작성하지 않는다.

**Acceptance**:

- AST/grep 정적 분석: `_memory_context.py`/`_reflexion.py`/Gap4 변경분이 명시된 기존 헬퍼를 import + 호출하며, 새 LLM 호출 프롬프트나 새 평가/ranking 함수를 정의하지 않음.
- `_reflexion.py` 가 자체 `LLMProvider.chat` 직접 호출을 하지 않고 `evaluator._call_llm` 경유함을 검증.

#### REQ-AGENT-V3-PERF-001 — 4 flag ON 시 성능예산 미초과 [P1]

**WHEN** 4 sub-flag 가 모두 ON,
**THE SYSTEM SHALL** SPEC-AGENT-V2-REACT 의 REQ-AGENT-PERF-HAPPY-001 (happy-path 턴 end-to-end p95 < 8s) 와 REQ-AGENT-PERF-EXHAUST-001 (exhausted p95 < 12s) 예산을 초과하지 않는다. 메모리 주입은 token budget 안에서, Reflexion 호출은 기존 iteration/turn_deadline 가드 안에서 수행된다.

**Acceptance**:

- 4 flag ON + 200턴 mixed happy-path 부하 → end-to-end p95 < 8s (V2 부하 테스트 하니스 재사용).
- 4 flag ON + 50턴 forced-exhaustion → p95 < 12s. Reflexion 추가 evaluator 호출이 turn_deadline 안에 흡수됨.
- Gap1 메모리 주입이 첫 LLM call latency 를 측정 — 단위 perf 테스트로 주입 조립 오버헤드 < 50ms.

#### REQ-AGENT-V3-SEC-001 — 메모리 주입 격리 + truncation [P1]

**THE SYSTEM SHALL** Gap1 메모리 컨텍스트 내 user-origin 텍스트 (recent turn 요약의 user_text/bot_text) 를 기존 `get_recent_history._summarize_payload` 의 cap (200자) 으로 제한하고, 메모리 블록 전체를 `[MEMORY CONTEXT — SYSTEM DERIVED]` 펜스로 명시 구획한다. 메모리 주입 페이로드도 기존 payload truncation 정책 (SPEC-CONVERSATION-LOG-001 REQ-LOG-PAYLOAD-CAP-001 의 2048자 등가 cap) 을 적용한다. `run_react_loop` 의 `_build_user_message` 가 사용하는 `[USER INPUT — DATA ONLY]` 펜스는 V2 그대로 무변경.

**Acceptance**:

- flag ON + 과거 turn 에 5000자 user_text → 주입 블록 내 해당 요약이 200자로 cap.
- flag ON + 악의적 user 입력 ("ignore previous instructions ...") 이 과거 turn 에 있음 → system 메모리 블록에 들어가나 `[MEMORY CONTEXT — SYSTEM DERIVED]` 펜스로 구획되고, 별도로 `_build_user_message` 의 `[USER INPUT — DATA ONLY]` 펜스는 V2 그대로 유지됨을 검증 (이중 격리).
- 메모리 블록 전체 char 길이 ≤ `AGENT_V3_MEMORY_MAX_TOKENS`*4 (REQ-AGENT-V3-MEM-CAP-001 와 연동).

---

## Environment Variables (introduced by this SPEC)

| Env var | Type | Default | Purpose |
|---|---|---|---|
| `AGENT_V3_MEMORY_INJECTION_ENABLED` | bool | `false` | Gap1 메모리 자동 주입 (REQ-AGENT-V3-MEM-FLAG-001) |
| `AGENT_V3_REFLEXION_ENABLED` | bool | `false` | Gap2 Reflexion 루프 (REQ-AGENT-V3-REFLEX-FLAG-001) |
| `AGENT_V3_PROACTIVE_ENABLED` | bool | `false` | Gap3 능동 제안 + suggest_next_step tool (REQ-AGENT-V3-PROACT-FLAG-001) |
| `AGENT_V3_DISLIKE_MEMORY_ENABLED` | bool | `false` | Gap4 크로스스레드 dislike (REQ-AGENT-V3-DISLIKE-FLAG-001) |
| `AGENT_V3_MEMORY_MAX_TOKENS` | int | `1500` | Gap1 메모리 주입 페이로드 token cap (REQ-AGENT-V3-MEM-CAP-001) |

4개 sub-flag 모두 production 기본 `false` — 단계적 롤아웃. `plan.md` 가 추가 튜닝 env (예: recent-N, proactive 임계 N) 도입 여부 결정.

---

## Exclusions (What NOT to Build)

각 항목은 별도 future SPEC. 본 SPEC 에서 명시적으로 제외:

1. **토큰 스트리밍 응답 (streaming responses).** `respond` tool 은 완성된 단일 메시지 발송. token-by-token Telegram 스트리밍 없음 — future SPEC.
2. **멀티에이전트 / 플래너 서브에이전트 spawn.** planner+worker 분리, sub-agent spawn 없음. single-loop single-LLM 유지 — V3 범위 외 (SPEC-AGENT-V2-REACT Non-Goal #1 계승).
3. **툴이 툴을 호출하는 컴포지션 (tool-calls-tool).** 툴은 leaf-level. 컴포지션은 LLM reasoning 레벨에서만. dispatch 함수 내부 tool 호출 금지.
4. **비용 인지 동적 툴 선택 (cost-aware dispatch).** LLM 에 per-tool cost 미고지. token budget cap 이 유일한 비용 가드 — cost-aware 선택은 V3 범위 외.
5. **V1 토폴로지 변경.** `app/graphs/fashion_bot.py` 의 V1 (`build_graph()` non-v2 경로) 무변경. byte-identical 유지가 제약. V3 flag 는 V1 에 영향 0.
6. **그래프 토폴로지 / 노드 추가·삭제.** `_build_graph_v2()` 무변경. 모든 V3 변경은 in-loop / registry / store 한정.
7. **WorkingState 스키마 변경.** V2 의 3필드 (`agent_iterations`/`tool_call_history`/`agent_status`) 로 충분. Gap 들은 ctx dict + store 로 처리 — 새 state 필드 추가 없음.
8. **evaluator 그래프 노드 재배선.** Gap2 는 evaluator **헬퍼** 만 in-loop 호출. evaluator 노드를 V2/V3 그래프에 배선하지 않음 (V1 전용 유지).
9. **TasteProfile dislike score semantics 변경.** Gap4 는 timestamp dict **추가** 만. `reinforce_disliked_*` 의 decay(0.9)/cap 로직 무변경.
10. **새 conversation_log event type.** `tool_call`(20th) 이미 존재. V3 는 새 이벤트 타입 추가 안 함.
11. **percentage / per-user 롤아웃.** sub-flag 는 binary (컨테이너 레벨). percentage 롤아웃은 operational SPEC (SPEC-AGENT-V2-REACT OQ-5 계승).
12. **음성/비디오 입력.** Telegram audio/video 미처리 (SPEC-AGENT-V2-REACT Non-Goal #19 계승).

---

## Stakeholders

| Role | Responsibility |
|---|---|
| Product / Founder (hchsa77@gmail.com) | 4-gap 우선순위 + sub-flag 독립 게이트 정책 확정. byte-identical-off 제약 승인. multi-agent/streaming 의 future-SPEC deferral 승인. |
| AI Server Owner (this SPEC) | `app/agents/{react_loop,_memory_context,_reflexion,tool_registry}.py`, `app/agents/tools/suggest_next_step.py`, `app/channels/{taste_profile,taste_profile_pg}.py`, `app/agents/tools/{update_taste,search_products,refine_search}.py`, `app/core/config.py`. 5 env. `tests/test_agent_v3/`. flag 롤아웃 runbook. |
| SPEC-AGENT-V2-REACT owner | 본 SPEC 이 V2 OQ-7 을 resolution. V2 spec.md 에 OQ-7 resolved 표기는 followup amendment (non-blocking). |
| SPEC-AGENT-V2-CLEANUP-001 owner | RC8 — V3 Gap2 가 evaluator 헬퍼를 live dependency 화. cleanup SPEC 을 "evaluator 노드 wiring 만 제거, 평가 헬퍼 보존" 으로 조정 (followup, V3 plan-audit 비차단). |
| SPEC-MEMORY-001 owner | Gap4 TasteProfile additive 확장이 Protocol freeze 를 침범하지 않음 확인 (Protocol 무변경, `update()` 시그니처 무변경). |
| dev-app Postgres operator | 새 테이블 없음. TasteProfile PG row 에 timestamp dict 추가로 row 크기 소폭 증가 (브랜드/키워드당 float 1개). |

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | Gap1 주입이 token budget 압박 → exhaustion 조기 트리거 | Medium | Medium | `AGENT_V3_MEMORY_MAX_TOKENS`(1500) cap + truncation. 주입은 첫 messages 1회만 — 누적 budget guard 정상 동작 (REQ-AGENT-V3-MEM-CAP-001). |
| R2 | Gap2 evaluator 입력 candidates 빈약 (top5 only) → 평가 부정확 | Medium | Low | `sess.last_results` 풀세트 사용 (plan.md OQ-V3-1). evaluator fail-open(score=1.0) 이 안전망. |
| R3 | Gap2 `EVALUATOR_TIMEOUT_S`(8s) > per-tool(5s) → turn_deadline / p95<8s 초과 | Medium | High | **잔여-budget timeout wrap + 강제 취소** (REQ-AGENT-V3-REFLEX-DEADLINE-001) — pre-check 만으로는 불충분, evaluator 호출이 `remaining = turn_deadline - now` 안에서 cancel. 추가로 SELF_CRITIQUE_MAX_ITERATIONS(2) 호출 cap (REQ-AGENT-V3-REFLEX-BOUND-001). |
| R4 | Gap3 신규 tool 이 REGISTRY 7→8 → flag OFF byte-identical 깨짐 | High | High | REGISTRY 빌드 시점 flag 분기 — OFF 시 8번째 미등록 + `_SYSTEM_PROMPT` 무변경. REQ-AGENT-V3-PROACT-FLAG-001 + REQ-AGENT-V3-COMPAT-ALLOFF-001 로 검증. |
| R5 | Gap4 TasteProfile 직렬화 호환 — PG store 새 dict 미인식 시 read 실패 | High | High | additive dict (default_factory). 구 row 역직렬화 시 빈 dict default. REQ-AGENT-V3-DISLIKE-SCHEMA-001 acceptance 로 검증. |
| R6 | 4 flag 조합 폭발 (2^4=16) — 회귀 표면 | Medium | Medium | all-off=V2 byte-identical 단일 가드 (REQ-AGENT-V3-COMPAT-ALLOFF-001) + 각 flag 독립 ON 시나리오만 인수 (조합 전수 아님). |
| R7 | RC8 — SPEC-AGENT-V2-CLEANUP-001 의 evaluator V2.1 제거와 Gap2 충돌 | High | Medium | spec.md 에 명시 + cleanup SPEC 조정 followup. Gap2 가 evaluator 헬퍼 import 를 명시적 live dependency 로 문서화. plan-audit 비차단. |
| R8 | Gap1 in_memory backend → get_recent_history 빈 list → 주입 무의미 | Low | Low | fail-soft placeholder. TasteProfile 은 in_memory 에서도 동작 — 최소 취향 주입 보장. |
| R9 | sub-flag 부분 ON 조합에서 예상치 못한 상호작용 (예: Gap2 ON + Gap4 ON) | Low | Medium | 각 Gap 이 독립 경로 (Gap2=ToolMessage 머지, Gap4=store/exclude). 직교 설계. plan.md 가 pairwise smoke 1-2개 추가. |

---

## Open Questions (deferred to plan.md / implementation)

**Resolved (recommendation adopted — REQ 본문이 이미 값 확정, 재오픈 금지):**

- **OQ-V3-2 — RESOLVED**: Gap3 신규 tool 이름 = `suggest_next_step` (권고안 채택; REQ-AGENT-V3-PROACT-TOOL-001 / AC-3.1 이 이 이름으로 확정).
- **OQ-V3-4 — RESOLVED**: Gap1 recent-N = **5** (권고안 채택; REQ-AGENT-V3-MEM-INJECT-001 이 N=5 로 확정).
- **OQ-V3-5 — RESOLVED**: Gap3 능동 제안 트리거 임계 `candidates_count < N` 의 N = **3** (권고안 채택; REQ-AGENT-V3-PROACT-PROMPT-001 이 N=3 으로 확정).

**Deferred (본 SPEC 승인을 막지 않음 — 코드 작성 전 plan.md 에서 결정):**

1. **OQ-V3-1**: Gap2 evaluator 입력 candidates = `sess.last_results` 풀세트(정확) vs `result.top_candidates` 5개(빠름). 권장: 풀세트 (evaluator fail-open 이 안전망). (참고: REQ-AGENT-V3-REFLEX-EVAL-001 이 풀세트로 잠정 명시 — plan.md 가 최종 확정.)
3. **OQ-V3-3**: Gap4 timestamp 입도 — per-key 단일 last-ts vs 이력 list. 권장: 단일 last-ts (storage 최소, recency discount 충분).
6. **OQ-V3-6**: Gap3 REGISTRY flag 분기 방식 — 모듈 로드 시점 분기 vs 매 turn 동적. 권장: 모듈 로드 시점 (byte-identical 검증 명확, settings 는 lifespan 고정).
7. **OQ-V3-7**: Gap4 recency 가중 함수 형태 (linear decay vs exponential). 권장: 기존 TasteProfile decay(0.9) 와 일관된 exponential.

---

## Cross-References

- **Increments (HARD)**: SPEC-AGENT-V2-REACT v0.1.1 — 모든 REQ 유효. V3 가 OQ-7 resolution + 4 REQ 그룹 추가. supersede 아님.
- **Builds on (SOFT)**:
  - SPEC-AGENTIC-CRITIQUE-001 — `evaluator._call_llm`/`_build_fastpath_delta`/`CritiqueScore` 를 Gap2 가 래핑. 알고리즘 무변경.
  - SPEC-MEMORY-001 — TasteProfile Protocol/`update()` 무변경; Gap4 는 additive dataclass 필드 확장만.
  - SPEC-CONVERSATION-LOG-001 — `tool_call`(20th) 이미 존재; Gap1 이 `get_recent_history` dispatch 래핑. catalog 무변경.
  - SPEC-CLARIFY-CARDS-001 — Gap3 `suggest_next_step` 이 clarify 카드 빌더 패턴 재사용.
- **Conflicts / followup (non-blocking — classification: non-blocking-for-run)**:
  - SPEC-AGENT-V2-CLEANUP-001 — RC8: cleanup SPEC 의 followup 조정 계약은 (1) evaluator 평가 헬퍼 모듈 (그래프 노드 wiring 만 제거), (2) **`SELF_CRITIQUE_*` env family 전체**, (3) **`EVALUATOR_*` env family 전체** 를 모두 보존해야 한다 (단순 헬퍼 보존만으로는 V3 Gap2 가 깨짐 — §"SPEC-AGENT-V2-REACT 와의 관계" 참조). cleanup SPEC 의 `AGENT_V2_REACT_ENABLED` 무조건화는 V3 master→sub-flag 게이트에 무해. V3 plan-audit 통과 후 followup.
- **Affected modules**: 위 Architecture Snapshot 참조.
- **Project context**: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md`.
- **Research basis**: `.moai/specs/SPEC-AGENT-V3-REACT/research.md` (코드 경로·라인 직접 검증, 2026-05-16).

---

## Implementation Notes

> 이 섹션은 Run phase 완료 후 추가됨 (2026-05-17). 기존 REQ/EARS 텍스트는 무변경.

### 실제 납품 파일 목록

**신규 소스 파일 (NEW, 3개)**:
- `app/agents/_memory_context.py` — Gap1 메모리 컨텍스트 빌더 (래핑 전용, 127 LOC)
- `app/agents/_reflexion.py` — Gap2 evaluator 래핑 헬퍼 (래핑 전용, 81 LOC)
- `app/agents/tools/suggest_next_step.py` — Gap3 8번째 tool (어댑터 재사용, ≤80 LOC)

**수정 소스 파일 (MODIFIED, 8개)**:
- `app/agents/react_loop.py` — Gap1 system message 분기 + Gap2 `_maybe_reflexion` 헬퍼 배선 + Gap3 `_PROACTIVE_DIRECTIVE` 상수 + system content 조립 순서
- `app/agents/tool_registry.py` — Gap3 flag-aware REGISTRY 빌드 (모듈 로드 시점, `_rebuild_registry_for_flag` test helper 추가), `SuggestNextStepArgs/Result` TypedDict
- `app/agents/tools/update_taste.py` — Gap4 dislike timestamp 기록 (flag-gated)
- `app/agents/tools/search_products.py` — Gap4 store dislike → exclude 머지 (flag-gated)
- `app/agents/tools/refine_search.py` — Gap4 동일 패턴
- `app/channels/taste_profile.py` — Gap4 `disliked_brands_ts`/`disliked_keywords_ts` additive 필드 + `recency_weighted_excludes` 헬퍼
- `app/channels/taste_profile_pg.py` — Gap4 UPSERT/SELECT/`_row_to_profile` 3-SQL 수정
- `app/core/config.py` — 5 env 추가 (4 sub-flag bool + `AGENT_V3_MEMORY_MAX_TOKENS` int)

**신규 Alembic 마이그레이션 (1개)**:
- `migrations/versions/0005_add_taste_dislike_ts.py` — `ai.user_taste_profile` 에 `disliked_brands_ts JSONB DEFAULT '{}'`, `disliked_keywords_ts JSONB DEFAULT '{}'` 컬럼 additive 추가

**신규 테스트 (11개, `tests/test_agent_v3/`)**:
- `conftest.py`, `_v2_baseline.py`, `test_byte_identical.py`, `test_config_v3.py`, `test_edge_orthogonality.py`, `test_gap1_memory.py`, `test_gap2_reflexion_bound.py`, `test_gap2_reflexion_deadline.py`, `test_gap2_reflexion_eval.py`, `test_gap3_proactive.py`, `test_gap4_dislike.py`, `test_performance_v3.py`, `test_security_v3.py`, `test_wrap_only.py`
- `tests/test_memory_pg/test_migration_0005.py`

### 마이그레이션 파일명 정정

tasks.md T9 에서 `0006_add_taste_dislike_ts.py` 로 계획되었으나, 실행 시점 `migrations/versions/` 최신 파일이 `0004_*` 임을 확인하여 **`0005_add_taste_dislike_ts.py`** 로 수정. "다음 sequential version" 지시가 권위적. 사실 정정이며 scope 변경 아님.

### Evaluator Fix-Cycle (평가 1회차 후 수정)

evaluator-active 초기 평가에서 2가지 블로킹 이슈 발견, 동일 사이클 내 해결:

1. **테스트 격리 (BLOCKING-1)**: 크로스 테스트 설정 누출로 일부 V3 테스트 오탐 발생. `tests/test_agent_v3/conftest.py` (`_v3_isolation`, autouse, function-scoped) 추가 — `get_settings` LRU 캐시 클리어 + 13개 설정 속성 스냅샷/복원 + taste-store 싱글톤 초기화 + flag-aware REGISTRY 재설정. 5개 V3 테스트 파일에서 중복 fixture 제거 (DRY). **프로덕션 코드 변경 0줄.**

2. **커버리지 미달 (BLOCKING-2)**: 에러 경로 / 브랜치 테스트 추가로 해결. V3 신규/변경 라인 100% 커버리지 달성.

추가 도출 항목:
- `tool_registry._rebuild_registry_for_flag(enabled)` — 테스트 전용 헬퍼. Gap3 REGISTRY가 모듈 로드 시점에 빌드되므로(OQ-V3-6 resolved), 테스트에서 플래그 전환 후 REGISTRY를 재현하기 위해 필요. 프로덕션 경로 무변경.
- `_v2_baseline.V2_TASTE_PROFILE_FIELD_REPRS` — AC-4.3 superset 단언용 frozen 상수.

### 테스트 / 품질 결과

| 항목 | 결과 |
|------|------|
| `uv run pytest` 전 스위트 | **810 passed / 9 pre-existing failed / 117 skipped** |
| Net regression | **0** (9 failures = 사전 존재 `.env`-driven 테스트, V3 scope 외) |
| V3 신규/변경 라인 커버리지 | **100%** |
| evaluator-active 점수 | **92.8/100 PASS** |
| manager-quality TRUST5 | **PASS** |
| ruff lint+format | **clean** |

### 미결 운영 항목 (deferred)

| 항목 | 분류 | 설명 |
|------|------|------|
| AC-P.1 200-턴 load smoke | 운영/수동 | 실 Telegram 환경 + 실 LLM 호출 필요. `test_performance_v3.py` 의 mock 기반 p95 어서션으로 구조적 보장 검증 완료. 운영 배포 후 수동 확인 |
| migration 0005 Docker 검증 | 운영/수동 | testcontainers 로컬 미지원. `test_migration_0005.py` 4개 테스트 Docker-skip (선례: `test_migration_0004`). dev-app postgres 배포 시 수동 `alembic upgrade head` 확인 필요 |
| **Gap4 배포 순서** | **운영 필수** | **마이그레이션 선행(alembic upgrade head) 후 코드 배포.** `taste_profile_pg._aget_or_create` 의 명시적 RETURNING SELECT 가 컬럼 존재를 요구함. 미-마이그레이션 DB에서 코드 선배포 시 hard fail. |

### 크로스-SPEC 라이브 의존성

SPEC-AGENT-V2-CLEANUP-001 의 followup 조정 계약은 다음을 **모두** 보존해야 V3 Gap2가 정상 동작함:
1. `evaluator._call_llm` / `_build_fastpath_delta` / `_evaluator_prompt.build_user_prompt` / `CritiqueScore` (그래프 노드 wiring 만 제거, 헬퍼 모듈 보존)
2. `SELF_CRITIQUE_*` env family 전체 (`MAX_ITERATIONS` / `THRESHOLD` / `TIMEOUT_S`)
3. `EVALUATOR_*` env family 전체 (`MODEL` / `MAX_TOKENS` / `TEMPERATURE` / `TIMEOUT_S`)

이 env들이 삭제되면 V3 Reflexion 루프가 런타임 오류. **SPEC-AGENT-V2-CLEANUP-001 followup PR 전 이 의존성 명시 필요.** (V3 merge의 blocker 아님 — 구현 완료 시점 기준.)
