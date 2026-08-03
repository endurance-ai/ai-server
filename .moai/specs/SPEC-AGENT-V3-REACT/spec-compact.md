# SPEC-AGENT-V3-REACT — Compact

`id: SPEC-AGENT-V3-REACT | v0.1.0 | draft | P1 | issue 0`
`labels: [agentic, react-loop, memory-injection, reflexion, proactive, dislike-memory, brownfield-delta, kiko-bot]`

**Increment (NOT supersede)** of SPEC-AGENT-V2-REACT. V2 ReAct loop (autonomous multistep orchestration) is DONE — V3 closes 4 gaps WITHOUT new graph topology. Each gap = independent sub-flag. 4 all-off ⇒ byte-identical V2. Wrap existing helpers; no reimplementation. Resolves V2 OQ-7 (evaluator wrapped in-loop, not as graph node).

## REQ + 인수기준 요약

| REQ-ID | 인수기준 핵심 |
|---|---|
| REQ-AGENT-V3-MEM-INJECT-001 (P1) | flag ON → TasteProfile(`boost_*`) + 최근 5턴(`get_recent_history` 래핑) 을 system 컨텍스트 자동 주입; LLM 명시 호출 불필요 |
| REQ-AGENT-V3-MEM-CAP-001 (P1) | 주입 ≤ `AGENT_V3_MEMORY_MAX_TOKENS`(1500) char-cap, 최신 우선, per-turn 32K budget 안 종속 |
| REQ-AGENT-V3-MEM-FLAG-001 (P0) | flag OFF → messages byte-identical V2, 메모리 경로 0 호출 |
| REQ-AGENT-V3-REFLEX-EVAL-001 (P1) | flag ON → search/refine 결과를 `evaluator._call_llm`/`_build_fastpath_delta` 래핑 평가 (재구현 금지) |
| REQ-AGENT-V3-REFLEX-DELTA-001 (P1) | quality delta 를 다음 LLM turn ToolMessage `_quality` 로 첨부 → LLM 자율 refine (강제 아님) |
| REQ-AGENT-V3-REFLEX-BOUND-001 (P0) | Reflexion 호출 횟수 `SELF_CRITIQUE_MAX_ITERATIONS`(2) cap, iteration counter 미증가, infinite-loop guard 무충돌 (history 미오염) |
| REQ-AGENT-V3-REFLEX-DEADLINE-001 (P0) | **D2**: Reflexion = `asyncio.wait_for(eval, timeout=remaining=max(0,turn_deadline-now))` 강제 wrap·취소. pre-check 불충분(`EVALUATOR_TIMEOUT_S`=8s>residual) — 취소-on-overrun normative, p95<8s/12s 기계 보장 |
| REQ-AGENT-V3-REFLEX-FLAG-001 (P0) | flag OFF → ToolMessage content byte-identical V2, evaluator 0 호출 |
| REQ-AGENT-V3-PROACT-TOOL-001 (P1) | flag ON → 8번째 tool `suggest_next_step` REGISTRY 등록 (adapter 재사용, ≤80 LOC) |
| REQ-AGENT-V3-PROACT-PROMPT-001 (P1) | flag ON → system prompt 능동성 지침 (약결과 `candidates_count<3` 선제 suggest / 모호 선제 clarify) |
| REQ-AGENT-V3-PROACT-FLAG-001 (P0) | flag OFF → `TOOL_NAMES` 길이 7 + `_SYSTEM_PROMPT` byte-identical V2 |
| REQ-AGENT-V3-DISLIKE-SCHEMA-001 (P1) | flag ON → TasteProfile additive dislike-timestamp dict (Protocol/`update()` 무변경, 직렬화 호환) |
| REQ-AGENT-V3-DISLIKE-DISCOUNT-001 (P1) | 저장 cross-thread dislike → 후속 search 자동 디스카운트 (기존 exclude 경로 재사용, recency 가중) |
| REQ-AGENT-V3-DISLIKE-FLAG-001 (P0) | flag OFF → TasteProfile schema/behaviour + exclude 입력 byte-identical V2 |
| REQ-AGENT-V3-COMPAT-ALLOFF-001 (P0) | 4 sub-flag all-off → 전체 byte-identical V2 (6 시나리오, 단일 회귀 가드) |
| REQ-AGENT-V3-COMPAT-WRAP-001 (P0) | evaluator/TasteProfile-store/conv_log 헬퍼 래핑만 — 신규 평가/메모리/ranking 로직 금지 (AST) |
| REQ-AGENT-V3-PERF-001 (P1) | 4 flag ON → REQ-AGENT-PERF-HAPPY-001(p95<8s)/-EXHAUST-001(p95<12s) 미초과 |
| REQ-AGENT-V3-SEC-001 (P1) | 메모리 주입 `[MEMORY CONTEXT — SYSTEM DERIVED]` 펜스 + `_summarize_payload` cap; `[USER INPUT — DATA ONLY]` 펜스 V2 무변경 (이중 격리) |

5 REQ 그룹 (MEM / REFLEX / PROACT / DISLIKE / 횡단) — 품질 제약 ≤5 충족 (DEADLINE-001 은 REFLEX 그룹 내 atomic 분리, 그룹 수 불변).

## 수정/신규 파일

- MODIFY: `app/agents/react_loop.py` (messages 조립 1지점, search/refine dispatch-후 훅 1지점 — Reflexion 은 `asyncio.wait_for(timeout=remaining turn budget)` 강제 wrap, `_SYSTEM_PROMPT` flag-aware — **루프 메커니즘 무변경**), `app/agents/tool_registry.py` (flag-aware 8th tool + TypedDict), `app/channels/taste_profile.py` (additive ts dict + `recency_weighted_excludes`), `app/channels/taste_profile_pg.py` (직렬화 포함), `app/agents/tools/{update_taste,search_products,refine_search}.py` (flag-gated 블록), `app/core/config.py` (5 env)
- NEW: `app/agents/_memory_context.py`, `app/agents/_reflexion.py`, `app/agents/tools/suggest_next_step.py`, `tests/test_agent_v3/`
- UNCHANGED (asserted): `app/graphs/fashion_bot.py` (토폴로지), `app/graphs/state.py` (WorkingState), `app/graphs/nodes/{agent,evaluator}.py` 본문, `app/observability/conversation_log.py`/`event_payloads.py`, `app/channels/taste_profile.py` 의 store Protocol/`update()`/`reinforce_*`/`exclude_brands`/`_cap`

## Env (신규 5)

`AGENT_V3_MEMORY_INJECTION_ENABLED` (bool=false) · `AGENT_V3_REFLEXION_ENABLED` (bool=false) · `AGENT_V3_PROACTIVE_ENABLED` (bool=false) · `AGENT_V3_DISLIKE_MEMORY_ENABLED` (bool=false) · `AGENT_V3_MEMORY_MAX_TOKENS` (int=1500). 마스터: `AGENT_V2_REACT_ENABLED` + `AGENT_LLM_MODEL`. 재사용(보존 필요): `SELF_CRITIQUE_MAX_ITERATIONS`/`THRESHOLD`/`TIMEOUT_S` + `EVALUATOR_MODEL`/`MAX_TOKENS`/`TEMPERATURE`/`TIMEOUT_S` (Gap2 live dependency — cross-SPEC 항 참조).

## Exclusions (What NOT to Build)

1. 토큰 스트리밍 응답 (streaming responses)
2. 멀티에이전트 / 플래너 서브에이전트 spawn
3. 툴이 툴을 호출하는 컴포지션 (tool-calls-tool)
4. 비용 인지 동적 툴 선택 (cost-aware dispatch)
5. V1 토폴로지 변경 (byte-identical 유지가 제약)
6. 그래프 토폴로지 / 노드 추가·삭제 (`_build_graph_v2` 무변경)
7. WorkingState 스키마 변경
8. evaluator 그래프 노드 재배선 (헬퍼만 in-loop 호출)
9. TasteProfile dislike score semantics 변경 (ts dict 추가만)
10. 새 conversation_log event type (`tool_call` 20th 이미 존재)
11. percentage / per-user 롤아웃 (binary flag)
12. 음성/비디오 입력

## Open Questions

**Resolved (recommendation adopted — REQ 본문이 값 확정, 재오픈 금지)**: OQ-V3-2 tool 이름=`suggest_next_step` · OQ-V3-4 recent-N=5 · OQ-V3-5 능동 임계 N=3.

**Deferred (plan.md 결정)**: OQ-V3-1 Reflexion 입력 candidates (권장·잠정 `sess.last_results` 풀세트) · OQ-V3-3 ts 입도 (권장 단일 last-ts) · OQ-V3-6 REGISTRY flag 분기 방식 (권장 모듈 로드 시점) · OQ-V3-7 recency 가중 (권장 exponential).

## 핵심 cross-SPEC 위험

R7/RC8 — SPEC-AGENT-V2-CLEANUP-001 의 "Env vars to deprecate" 가 `SELF_CRITIQUE_*` family 전체 + `AGENT_V2_REACT_ENABLED` deprecate + `evaluator.py` 삭제 대상. V3 Gap2 가 (1) evaluator 평가 헬퍼 모듈, (2) **`SELF_CRITIQUE_*` env family 전체**, (3) **`EVALUATOR_*` env family 전체** 를 live dependency 화. cleanup followup 조정 계약은 이 3가지를 **모두** 보존해야 함 (단순 "헬퍼 보존" 불충분). `AGENT_V2_REACT_ENABLED` 무조건화는 V3 master→sub-flag 게이트에 무해. classification: non-blocking-for-run, V3 plan-audit 비차단.
