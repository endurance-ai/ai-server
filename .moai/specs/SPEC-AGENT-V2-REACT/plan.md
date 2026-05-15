# Plan: SPEC-AGENT-V2-REACT v0.1.1 — ReAct Loop + Tool Registry

## 0. Status & Prerequisites

- **SPEC version**: v0.1.1 (plan-auditor iteration 1/3 통과, composite 0.95; D1/D2 minor 반영 완료)
- **Status**: draft
- **Priority**: P0
- **Methodology**: DDD (Domain-Driven Development) — `.moai/config/sections/quality.yaml` `development_mode: ddd`. ANALYZE-PRESERVE-IMPROVE 사이클을 모든 brownfield task에 적용. 신규 모듈(`app/agents/**`)은 PRESERVE 단계가 없으므로 ANALYZE → IMPROVE 만 적용 (greenfield 내부 미니 사이클).
- **Audit**: spec.md 통과. plan.md 본 문서가 OQ 1~10 결정 + Task decomposition + Test strategy + Rollout 까지 완결시킨다. plan-auditor 차회 iteration 입력.

### HISTORY

- **2026-05-15 (runtime verification correction)**: OQ-1 의 false premise *"nova-pro (Bedrock): 본 인프라에 Bedrock 자격증명 미설정"* 을 폐기. Bedrock Nova 는 본 프로젝트 primary LLM (LiteLLM proxy 에 자격증명 구성됨). OQ-1 기본 모델을 `gpt-4o-mini` → `nova-lite` 로 정정. OQ-2 에 Bedrock `tool_choice` 거부 caveat + `llm_client.py` fix 문서화. raw curl + 실제 `.ainvoke()` 로 `nova-lite` tool calling 정상 동작 검증 완료.
- **2026-05-15 (topology-integration redesign)**: 실 Telegram 런타임에서 4개 결함 확인 (route_text V2 미게이팅, empty-input agent 환각, sticky-lang URL flip, V1 state-machine 잔존). T-006/T-007 재설계 + T-007.5(lang URL guard) + T-015(topology-integration redesign) 신설. 총 task **15 → 17** (T-000~T-015 + T-007.5). pick-callback 조사 결론: "empty webhook"은 drop 된 탭이 아니라 별개 contentless Update — pick 경로는 정상, 무수정. OQ resolution 전부 유효 (OQ-4 는 §15 Decision 1/4 로 구현 완성). 무효화된 OQ 없음.

### Blocking prerequisites

1. **[HARD BLOCKER] SPEC-CONVERSATION-LOG-001 v0.3.0 amendment PR** (REQ-AGENT-LOG-EVENT-001). `ai.log_conversation_event` 의 카탈로그에 `tool_call` (20번째 이벤트 타입) 을 추가하고 `app/observability/conversation_log.py` 에 `ToolCallPayload` TypedDict 를 export. 본 SPEC 의 implementation PR 은 amendment PR merge 이전에 절대 merge 불가. plan.md 의 Task 0 으로 분리.
2. **[SOFT] SPEC-AGENT-001 v2.0 amendment** — post-onboarding 토폴로지 supersede 문구 추가. cutover 후 follow-up PR. plan-audit 차단 요소 아님.

### Inputs used

- `.moai/specs/SPEC-AGENT-V2-REACT/spec.md` (1272 lines, full read)
- `app/graphs/state.py` (current `WorkingState` shape — extension target)
- `app/graphs/fashion_bot.py` (current 18-node topology, conditional edges)
- `app/graphs/routing.py` (9 routing functions — 4 to remove)
- `app/graphs/nodes/ingest.py` (Step C 삽입 지점 — head 80 lines)
- `app/graphs/nodes/respond.py` (`_Flow` enum 위치 — head 80 lines)
- `app/observability/conversation_log.py` (`emit` / `log_event` 패턴)
- `CLAUDE.md` (layer responsibilities, file map, env var convention)
- Recent commits: `5ff58a3` (plan-auditor iter1 fix), `3fc518c` (noscroll P0), `6fee80a` (demo), `d32b61b` / `25ce8ca` (SPEC-IMPLICIT-FB-001)

### Grounding correction (vs SPEC §Affected modules)

SPEC §Cross-References 에서 `app/graphs/nodes/router_text.py` 를 deprecated 대상으로 명시했으나, 실제 코드베이스에는 별도 파일이 존재하지 않는다. **현재 토폴로지**:

- `app/channels/router.py::route_text` — LLM 4-way 분류 호출 (RoutedDecision 생성)
- `app/graphs/nodes/ingest.py` — text branch 진입 시 `route_text` 호출
- `app/graphs/fashion_bot.py::_router_text_passthrough` (lines 121–126) — 그래프 노드는 단순 passthrough; routing.py 의 `_route_after_router_text` 가 결정

따라서 본 SPEC 의 "router_text 노드 deprecation" = (a) `fashion_bot.py` 의 passthrough 노드 제거 + (b) `routing.py::_route_after_router_text` 제거 + (c) `app/channels/router.py` 는 V2.0 동안 retained (다른 호출자 가능성 zero 검증 후 V2.1 cleanup SPEC 에서 제거). 본 plan.md 의 Task 14 에 명시.

---

## 1. Open Question Resolutions

각 OQ 는 **Decision · Rationale · Tradeoff accepted · Reversibility · plan-audit 차회 input** 4-tuple 로 결정. 미결 항목 없음.

### OQ-1: Agent loop LLM model selection

**Decision**: 기본 `nova-lite` (AWS Bedrock `us.amazon.nova-2-lite-v1:0`, via LiteLLM proxy `LITELLM_BASE_URL` — **본 프로젝트의 primary LLM**, `VISION_MODEL` / `RESPONSE_MODEL` 과 동일 proxy + 동일 alias). 환경변수 `AGENT_LLM_MODEL` 로 운영자가 override 가능 (`gpt-4o-mini` 는 동일 proxy 의 valid alternative alias). 미설정 시 `AGENT_V2_REACT_ENABLED` 가 효과적으로 false (fail-closed).

> **[Plan-phase error correction — 2026-05-15]**: 본 OQ 의 직전 버전은 기본 모델을 `gpt-4o-mini` 로 결정하고 그 근거로 *"nova-pro (Bedrock): 본 인프라에 Bedrock 자격증명 미설정"* 이라 기재했다. **이 전제는 사실과 다르다.** 본 인프라는 Bedrock 자격증명이 LiteLLM proxy 에 이미 구성되어 있으며, Bedrock Nova 가 이 프로젝트의 primary LLM (`VISION_MODEL=nova-lite`, `RESPONSE_MODEL`) 이다. runtime verification (raw curl + 실제 `.ainvoke()`) 으로 `nova-lite` 가 LiteLLM proxy 경유 tool calling 을 정상 지원함을 확인했다. 잘못된 전제를 폐기하고 기본값을 `nova-lite` 로 정정한다.

**Rationale**:
- **인프라 정합성 (정정된 사실)**: Bedrock Nova 는 본 프로젝트의 primary LLM. `app/channels/vision.py` (`VISION_MODEL`), `app/graphs/nodes/respond.py` (`RESPONSE_MODEL`) 가 이미 동일 LiteLLM proxy 경유 `nova-lite` 사용. agent loop 도 동일 모델 사용 시 인프라/관측/비용 모델 일관성 확보, 추가 인프라 0.
- LiteLLM proxy 호환: 본 프로젝트는 이미 `respond.py` 와 `ask_clarify.py` 에서 `langchain-openai` ChatOpenAI 를 LiteLLM proxy 경유로 사용 (확인된 패턴). `nova-lite` / `gpt-4o-mini` 모두 동일 proxy alias 로 라우팅 가능.
- Tool calling 검증 (runtime): raw `POST /v1/chat/completions` `model=nova-lite` + `tools=[...]` → 200, well-formed `tool_calls` 반환 (`finish_reason: tool_calls`). 실제 `bind_tools().ainvoke()` 로도 `search_products` tool call 정상 생성 확인. 본 SPEC 의 7-tool registry 는 단순 (axis 6개 + 3-4 args/tool) — `nova-lite` 로 충분.
- 비용: Bedrock Nova Lite 는 gpt-4o-mini 동급 또는 더 낮은 토큰 단가. 6-iteration 한턴 토큰 예산은 R4 (cost explosion) 마지노선 안 (기존 분석 유지).
- Latency: Nova Lite 는 저지연 tier. 6 iter + tool dispatch 합산이 REQ-AGENT-PERF-EXHAUST-001 (12s budget) 안 (기존 분석 유지).
- `gpt-4o-mini` (alternative): 동일 proxy 의 valid alias 로 남겨둠 — `AGENT_LLM_MODEL=gpt-4o-mini` 한 줄로 swap 가능 (단 OpenAI 계정 quota 별도). high-stakes escalation 은 별도 SPEC.
- gpt-4o (full): 8배 비용. 본 SPEC 7-tool 단순도에서 ROI 부족. 별도 SPEC.

**Tradeoff accepted**: Nova 는 OpenAI 대비 일부 OpenAI-Tools 파라미터 (`tool_choice`) 를 거부 (HTTP 400). Mitigation: OQ-2 의 fix 로 `tool_choice` 미전송 보장 (`nova-lite` AND `gpt-4o-mini` 모두 정상 동작). 그 외 tool selection 정확도 모니터링은 R10 의 SQL (per-tool selection 분포) 로 유지.

**Reversibility**: HIGH. `AGENT_LLM_MODEL` 환경변수 한 줄. 컨테이너 재시작이면 충분.

### OQ-2: OpenAI Tools API vs JSON-mode parser

**Decision**: **OpenAI Tools API** (structured tool calls via `tools=[...]`) 를 사용. `langchain_openai.ChatOpenAI.bind_tools()` 사용. **Bedrock caveat: `tool_choice` 파라미터를 절대 전송하지 않는다** (Bedrock 이 HTTP 400 으로 거부 — `litellm.UnsupportedParamsError: bedrock does not support parameters: ['tool_choice']`).

**Rationale**:
- 본 프로젝트 의 `respond.py` / `ask_clarify.py` 는 이미 `ChatOpenAI` (LangChain) 사용 — symmetry 보장.
- Tools API 는 schema-enforced output → JSON malformation 발생률 < 0.1% (vs JSON-mode parser ~3-5%). R2 (LLM JSON malformation) mitigation 강도 ↑.
- TypedDict → OpenAI tools schema 변환은 자동 (Pydantic v2 → JSON Schema → tools schema) — Task 2 의 registry 가 single source.
- LiteLLM proxy 는 OpenAI Tools API 를 투명하게 pass-through (확인됨). Bedrock Nova 는 `tools=[...]` 자체는 정상 지원 (runtime 검증: well-formed `tool_calls` 반환) — 단 `tool_choice` 파라미터만 거부.
- `tool_choice` 미전송은 OpenAI 의 default behavior (`"auto"`) 와 functionally equivalent — ReAct loop 은 모델이 자율적으로 tool 선택 또는 `respond` 로 종료하는 데 의존하므로 `tool_choice` 가 불필요. 따라서 `nova-lite` AND `gpt-4o-mini` 양쪽 모두에서 동작 (모델 swap 가능성 유지).

**Bedrock 호환 fix (구현됨, Task 4 — `app/agents/llm_client.py`)**:
- Runtime 검증으로 installed `langchain-openai 0.3.34` 의 `bind_tools()` 는 `tool_choice` 인자 미전달 시 request body 에 `tool_choice` 를 **주입하지 않음** 을 확인 (source: `if tool_choice:` 가 falsy 면 skip; 실제 `.ainvoke()` 로 `nova-lite` 400 미발생 검증).
- 그럼에도 향후 langchain 버전 변경에 대비해 명시적 방어를 적용: (i) `bind_tools(..., tool_choice=None)` 로 의도 명시 + 미주입 계약 고정, (ii) `ChatOpenAI(extra_body={"drop_params": True})` 로 LiteLLM 이 provider-unsupported 파라미터를 silently strip 하도록 지시 (어떤 레이어가 `tool_choice` 를 주입하더라도 Bedrock 400 대신 drop). 둘 다 OpenAI 에 no-op → `AGENT_LLM_MODEL` swappable 유지.

**Tradeoff accepted**: 비OpenAI 모델 (예: Claude Sonnet via Bedrock) 으로 swap 시 JSON-mode 폴백 필요 가능성. Mitigation: `app/agents/react_loop.py` 내부에 `_llm_invoke()` 추상화 인터페이스를 두어 V3 에 abstraction 추가 여지 유지. **본 SPEC 에서는 단일 구현 (OpenAI Tools)** 만.

**Reversibility**: MEDIUM. JSON-mode fallback 으로 swap 시 Task 4 (react_loop.py) 의 LLM invocation 한 함수 교체 + corrective retry 메시지 추가. 1-2일 작업.

### OQ-3: Vision 노드 subsume vs deterministic pre-agent step

**Decision**: **Vision 노드 유지** (deterministic pre-agent step). 사진/URL 포함 webhook → `resolve_image` → `vision_node` → (vision_result 가 WorkingState 에 채워진 채로) `agent` 노드 진입.

**Rationale**:
- Vision 호출은 **결정형** — 입력 = URL, 출력 = VisionResult JSON. 자율성 0 (SPEC §Background table 의 LLM 자율성 분석).
- Subsume 시 agent 의 첫 iteration 이 항상 `analyze_image` 호출 → LLM 1회 추가 호출 + 1 iteration 소진 + tool dispatch overhead = ~1.5s 추가 latency × 100% photo turn. ROI 음수.
- 유지 시 agent 첫 iteration 컨텍스트에 `vision_result` 이미 들어 있음 → LLM 은 즉시 `search_products` / `ask_user_clarification` 결정 가능. happy-path 평균 iteration 수 ↓ → 비용 ↓.
- SPEC §Open Questions OQ-3 의 preference ("subsume only if OQ-1 model has high tool-selection accuracy") — gpt-4o-mini 의 image-related tool 선택 정확도는 검증되지 않음. 보수적 결정.
- `analyze_image` tool 은 registry 에 **남겨둔다** — 향후 multi-step (예: agent 가 검색 후 추가 photo 분석 필요 판단) 시 사용 가능. Registry entry 는 dead code 가 아니다.

**Tradeoff accepted**: Photo turn 의 agent 첫 진입 시점이 Vision 의 4-5s latency 이후로 고정 → agent 의 "느낌상 즉답" UX 가 photo flow 에는 부재. 사용자 경험 분석상 photo 자체가 "찾기" 의도이므로 OK.

**Reversibility**: HIGH. fashion_bot.py 의 edge 한 줄 변경 (`vision_node → agent` 를 `vision_node → END (agent invokes analyze_image)` 로). Task 4-5 에 영향 없음. Subsume 으로 전환 시 Task 6 의 routing 만 수정.

### OQ-4: 온보딩 완료 직후 첫 turn destination

**Decision**: **Agent 즉시 진입, 첫 `respond` 가 contextual greeting 을 자연스럽게 포함**. 별도 explainer 메시지 노드 도입 안 함.

**Rationale**:
- 사용자 결정 (SPEC HISTORY v0.1.0) — agentic UX 가 본 SPEC 핵심. 첫 턴에 결정형 explainer 끼우면 self-defeating.
- 첫 턴 시스템 프롬프트에 `"This is the user's first message after completing onboarding. Greet briefly + acknowledge their preferences (from TasteProfile) + invite first request."` 한 줄 추가 → agent LLM 이 자연 발화로 처리.
- `get_recent_history` tool 로 onboarding 이벤트 (mood/color/fit/pinterest 선택값) 접근 가능 — agent 가 필요시 reference.
- 결정형 explainer 는 SPEC-CLARIFY-CARDS-001 / SPEC-ONBOARD-CARDS-001 의 "결정형 카드" 라인과는 다른 영역 — 그쪽은 입력 카드, 이쪽은 출력 발화. 출력은 자연어가 정답.

**Tradeoff accepted**: 첫 turn LLM 발화 변동성 ↑ — explainer 의 표준 톤이 깨질 위험. Mitigation: system prompt 에 첫 turn 디텍션 (`Session.last_agent_turn_no == 0`) + few-shot example 한 줄.

**Reversibility**: HIGH. Task 4 의 system prompt 한 줄. 코드 변경 없이 prompt 만.

### OQ-5: Feature flag rollout — binary vs percentage

**Decision**: **V2.0 binary only**. `AGENT_V2_REACT_ENABLED` true/false. Percentage rollout 은 V2.1 operational SPEC 으로 deferral.

**Rationale**:
- V2.0 목표 = ReAct 패턴의 **정합성 검증**. Percentage 도입은 검증 노이즈 (어떤 사용자가 어느 path 인지 attribution 복잡).
- Dev → Prod cutover 운영 시퀀스:
  1. Dev 에 `true` 영구 (SPEC-AGENT-V2-REACT plan-audit 통과 후 Task 13 의 manual smoke).
  2. 24-48h dev burn-in (정상 turn distribution 모니터링).
  3. Prod 에 `true` flip (low-traffic window, e.g., KST 03:00-05:00).
  4. 24h prod 모니터링 — abort 시 flag false + 컨테이너 재시작.
- 10K turns/day 인프라 규모에서 binary cutover 의 blast radius 는 1-hour rollback window 안 → 통제 가능.
- User-keyed bucketing 도입 시 추가: (a) bucketing function (md5(user_key) % 100), (b) flag-state-per-bucket 로깅 to conv_log, (c) A/B 분석 SQL. 모두 V2.1 operational SPEC.

**Tradeoff accepted**: Cutover blast radius = 100% 사용자 동시 전환. Mitigation: (a) R5 의 flag false 즉시 revert, (b) Task 13 cutover runbook 에 명시.

**Reversibility**: HIGH. Flag flip 한 줄 + 컨테이너 재시작.

### OQ-6: Resume protocol — accept loss vs persist after each iter vs new table

**Decision**: **(a) Accept loss**. 컨테이너 SIGKILL 시 partial `WorkingState` 손실 수용. 단, **(b) 의 부수효과** 활용: `tool_call_history` 는 매 iter 마다 `tool_call` 이벤트로 conv_log 에 이미 영속화됨 → 사후 디버깅용 trace 는 자동 확보.

**Rationale**:
- 현 webhook 처리 모델 (`ainvoke` per webhook, no checkpointer — SPEC-AGENT-001 acceptance #4) 는 이미 partial-state-loss 를 허용. Agent 만 다른 규칙으로 운영하면 일관성 깨짐.
- Resume 도입 시 비용: (1) `ai.agent_turn_state` 신규 테이블 + (2) 매 iter persistence (50ms × 6 = 300ms 추가 latency, R3 위배) + (3) resume-on-startup 로직.
- Webhook turn 의 평균 latency 8s 이내 + 컨테이너 SIGKILL 빈도 < 1/day (관측치, infra metric) → expected loss < 1 turn/day. ROI 음수.
- 사후 디버깅 needs (Langfuse trace + conv_log row)는 옵션 (b) 의 부산물로 이미 만족 — operator 가 SQL 로 partial tool_call_history 재구성 가능.

**Tradeoff accepted**: 컨테이너 SIGKILL mid-loop 시 해당 user 의 다음 turn 은 fresh state — 약간의 UX 단절. Mitigation: `get_recent_history` tool 이 직전 turn 의 context 일부 복원 가능.

**Reversibility**: MEDIUM. 필요 시 별도 SPEC `SPEC-AGENT-RESUME-001` 로 `ai.agent_turn_state` 테이블 + persistence hook 추가. 본 SPEC 의 `tool_call_history` 형식이 그대로 사용 가능 → swap cost 낮음.

### OQ-7: `refine_search` evaluator 흡수 (α) vs 별도 그래프 노드 유지 (β)

**Decision**: **Option α (fold)**. `refine_search` tool 내부에서 evaluator 호출 가능. `app/graphs/nodes/evaluator.py` 그래프 노드는 **deprecated** — body 는 `app/agents/tools/refine_search.py` 의 helper 로 옮긴다. 본 SPEC 의 deprecated 노드 목록에 `evaluator` 추가 → **총 5개 deprecated** (router_text passthrough, critique_apply, taste_update, respond, evaluator).

**Rationale**:
- 현 evaluator 의 호출 빈도 (관측 가능치 — `SELECT count(*) FROM ai.log_conversation_event WHERE event_type='search_done' AND (payload->>'critique_retry_count')::int > 0` 기준, Langfuse trace 의 evaluator span 비율 ~15% — Task 0 amendment 후 정밀 측정 가능). 일관성 낮음 — 결정형 retry decision 이 LLM 의 자율 결정으로 흡수되는 게 더 자연스러움.
- Α 채택 시 architecture 단순화: search → respond 직결, 재시도는 LLM 의 자율 결정 ("이 결과 부족하니까 refine_search 호출"). Reflexion loop 가 ReAct loop 와 같은 구조에서 동작.
- Β 유지 시 모순 발생: agent 의 외부에서 retry 결정 (evaluator) + agent 내부에서도 retry 결정 (LLM 의 refine_search 선택) → 이중 retry 가능성 + 디버깅 복잡.
- SPEC R3 (Latency stacking) 영향: α 는 LLM call 1회 추가 (refine_search 호출 결정), β 는 evaluator LLM call 1회. 본 SPEC OQ-1 의 gpt-4o-mini 사용 시 차이 minimal.

**Tradeoff accepted**: SPEC-AGENTIC-CRITIQUE-001 의 `critique_retry_count` / `critique_trail` 등 4-가드 (iteration cap, stagnation, score regression, timeout) 는 agent loop 의 6-iter cap + 3-consecutive guard 로 대체. SPEC-AGENTIC-CRITIQUE-001 의 결정형 guard 가 LLM 자율 결정에 흡수됨. **본 SPEC 의 REQ-AGENT-FAILURE-INFINITE-001 + REQ-AGENT-LOOP-ITERATION-001 으로 동등한 안전망 확보**.

**Reversibility**: MEDIUM. β 로 전환 시 evaluator.py 를 deprecated 에서 retained 로 복귀 + routing.py 에 search → evaluator → agent 엣지 복원. Task 5-6 영향. 1주 작업.

**Cross-SPEC impact**: SPEC-AGENTIC-CRITIQUE-001 의 일부 REQ 가 본 SPEC 의 REQ 로 사실상 supersede. V2.1 cleanup SPEC 에서 SPEC-AGENTIC-CRITIQUE-001 의 deprecation 또는 absorption note 명시.

### OQ-8: `get_recent_history` payload shape per event_type

**Decision**: 이벤트 타입별 **selected keys whitelist** 적용. Full payload 절대 반환 안 함 (LLM context 부담 + privacy).

**Per-event-type whitelist** (Task 3f 에서 enforce):

| event_type | result.events[].payload_summary keys |
|---|---|
| `user_text` | `text` (cap 200 chars) |
| `user_photo` | `image_url_hash` (sha256 first 16 chars; no full URL) |
| `user_callback` | `callback_data` |
| `intent_routed` | `intent`, `critique_delta_summary` |
| `vision_done` | `style_node_primary`, `mood[:3]`, `items_count` |
| `search_done` | `query.text_query` (cap 100), `top_k_product_ids[:5]`, `raw_count` |
| `diversify_done` | `final_count` |
| `card_sent` | `product_ids[:5]`, `card_type` |
| `card_clicked` | `product_id`, `position` |
| `bot_text` | `text` (cap 200 chars) |
| `taste_update` | `applied`, `new_top_brands[:3]`, `new_top_keywords[:3]` |
| `tool_call` | `tool_name`, `iteration_no`, `latency_ms`, `error` (full payload.args / result_summary 제외 — recursive 방지) |
| `ask_clarify_sent` | `axis` |
| `node_error` | `node_name`, `exception_type`, `recovered` |
| Other 5 (기타) | `event_type` 만 |

**Rationale**: 사용자 PII (raw URL, full text) 노출 최소화 + LLM context token budget 보호 (per event ~50 tokens × 5 events = 250 tokens, 부담 적음).

**Tradeoff accepted**: LLM 이 full 정보 필요 시 별도 tool (e.g., `get_full_event(event_id)`) 가 필요할 수 있음. **본 SPEC 범위 외** — V2.1 에서 필요 시 추가.

**Reversibility**: HIGH. whitelist 는 `app/agents/tools/get_recent_history.py` 의 한 dict.

### OQ-9: `respond` tool 의 `cards` arg — pass-through (V2.0) vs LLM curation

**Decision**: **V2.0 full pass-through**. LLM 은 prior search/refine 결과의 `candidates` list 를 그대로 `respond(cards=...)` 에 전달. LLM curation 은 V2.1 enhancement.

**Rationale**:
- LLM curation 도입 시 (a) curation prompt 추가 + (b) curation decision 검증 (e.g., LLM 이 brand 균등 분포 깰 수 있음) + (c) diversify 로직 (`app/pipeline/diversify.py`) 와의 중복.
- Diversify 는 이미 다양성 캡 + tolerance 적용한 post-processed list — LLM 추가 curation 의 marginal value 낮음.
- V2.0 의 목표는 ReAct 패턴 검증, 카드 큐레이션은 직교 문제.

**Tradeoff accepted**: 사용자 turn 의 맥락 (e.g., "더 저렴한 거" 후 검색) 에 대한 카드 set 의 fine-tuning 은 search filter (price) 의 책임에 남김. LLM 이 cards 를 골라낼 수 없으므로.

**Reversibility**: HIGH. V2.1 에서 `cards: list[Candidate] | None` 의 sub-selection 로직만 추가.

### OQ-10: `tool_call_history` size in LLM context

**Decision**: **Pass-all (최대 6개) without truncation**. iteration cap 이 6 이므로 history 도 최대 6 entries. 6 × ~500 tokens/entry (truncated args + result_summary) ≈ 3K tokens — 32K budget 안 충분.

**Rationale**:
- 6-iter cap 의 small bound 가 자연적 cap. Truncation/summarization 도입은 over-engineering.
- Full history 가 LLM 의 자기 reasoning 일관성에 도움 (e.g., "이미 X 시도했으니 Y 시도하자").
- REQ-AGENT-SEC-PAYLOAD-001 의 cap (2048 chars/string, 50 items/list, 100 keys/dict) 가 이미 entry-level 에 적용됨 — 추가 cap 불필요.

**Tradeoff accepted**: V2.0 이후 도구 추가로 tool 정의 자체가 커지면 (R16) per-iter context 가 커질 수 있음. Mitigation: REQ-AGENT-PERF-TURN-BUDGET-001 의 32K cap 이 catch-all.

**Reversibility**: HIGH. Truncation 도입 시 `app/agents/react_loop.py` 의 context builder 에 sliding-window 한 줄.

### Decision summary table (plan-audit input)

| OQ | Decision | Reversibility | Cross-SPEC impact |
|---|---|---|---|
| 1 | `nova-lite` (Bedrock, primary LLM) default, fail-closed; `gpt-4o-mini` alt alias | HIGH | None (plan-phase Bedrock premise corrected 2026-05-15) |
| 2 | OpenAI Tools API via `bind_tools`; `tool_choice` NOT sent (Bedrock 400) — fix in `llm_client.py` | MEDIUM | None |
| 3 | Vision 노드 유지 (pre-agent) | HIGH | SPEC-VISION-UNIFY-001 untouched |
| 4 | Agent immediate, contextual greeting | HIGH | None |
| 5 | V2.0 binary, V2.1 percentage | HIGH | Future operational SPEC |
| 6 | Accept loss + free-side trace via tool_call | MEDIUM | None |
| 7 | Fold evaluator → refine_search (α) | MEDIUM | SPEC-AGENTIC-CRITIQUE-001 supersede (partial) |
| 8 | Per-event-type whitelist | HIGH | None |
| 9 | V2.0 full pass-through | HIGH | None |
| 10 | Pass-all (≤6 entries) | HIGH | None |

---

## 2. Task Decomposition

총 **17 tasks** (Task 0–15 + Task 7.5). Task 0–14 는 구현 완료 (Wave 1-8). Task 6/7 은 §15 redesign 으로 보강, Task 7.5 + Task 15 는 런타임 결함 4종 대응 신설. 모든 task 는 atomic DDD 또는 ANALYZE→IMPROVE 사이클로 완결 가능. 진행은 TodoWrite 로 동기.

### Task 0 — [BLOCKER] SPEC-CONVERSATION-LOG-001 v0.3.0 amendment PR

- **ID**: T-000
- **Description**: SPEC-CONVERSATION-LOG-001 `tool_call` event 카탈로그 +1 amendment. 본 SPEC 의 implementation PR merge 차단 해제 prerequisite.
- **REQ mapping**: REQ-AGENT-LOG-EVENT-001
- **Dependencies**: 없음 (parallel-startable)
- **Planned files (별도 PR, 본 SPEC 디렉토리 외부)**:
  - MODIFY: `.moai/specs/SPEC-CONVERSATION-LOG-001/spec.md` (HISTORY +1 entry, REQ 카탈로그 +1)
  - MODIFY: `app/observability/conversation_log.py` (`ToolCallPayload` TypedDict export, event_type whitelist 갱신)
  - ADD: 1 unit test in `tests/test_conversation_log.py` (round-trip + truncation)
- **Acceptance criteria**:
  - `from app.observability.conversation_log import ToolCallPayload` 성공.
  - SPEC-CONVERSATION-LOG-001 HISTORY 에 v0.3.0 entry 존재 + 본 SPEC ID 명시.
  - 기존 19개 이벤트 타입 테스트 모두 green.
  - PR merge 완료 + main branch HEAD.
- **Cross-SPEC ownership**: SPEC-CONVERSATION-LOG-001 owner. 본 SPEC owner 가 amendment PR 작성, conv-log owner review.

### Task 1 — `WorkingState` +3 필드 (ANALYZE-PRESERVE-IMPROVE)

- **ID**: T-001
- **Description**: `app/graphs/state.py` 의 `WorkingState` 에 `agent_iterations` / `tool_call_history` / `agent_status` 3 필드 추가. 기존 필드 변경 없음.
- **REQ mapping**: REQ-AGENT-COMPAT-STATE-001
- **Dependencies**: 없음
- **Planned files**:
  - MODIFY: `app/graphs/state.py`
  - ADD: `tests/test_agent_v2/test_state_extension.py` (snapshot test of model_fields)
- **DDD ANALYZE**: 현 `WorkingState` 필드 27개 (line 78–122). `extra="forbid"` + `arbitrary_types_allowed=True`. 기존 `_LIST_ADD` reducer 패턴 학습 — `tool_call_history` 도 동일 reducer 적용 (LangGraph state merge 시 append-only).
- **DDD PRESERVE**: snapshot test — 새 필드 추가 전 `set(WorkingState.model_fields.keys())` 캡처. 기존 27 필드의 type/default/optionality 회귀 0.
- **DDD IMPROVE**: 3 필드 추가:
  ```python
  agent_iterations: int = 0
  tool_call_history: Annotated[list[dict[str, Any]], _LIST_ADD] = Field(default_factory=list)
  agent_status: Literal["running", "done", "exhausted"] = "running"
  ```
- **Acceptance criteria**: 새 필드 정확히 3개. snapshot diff = +3 entries / -0 entries / -0 changes. `pytest` baseline (Task 9 이전) 100% green.

### Task 2 — `tool_registry.py` skeleton (greenfield ANALYZE→IMPROVE)

- **ID**: T-002
- **Description**: `app/agents/__init__.py` 생성 + `app/agents/tool_registry.py` 신규. 7 tools 의 TypedDict args/result + REGISTRY dispatch table + 공용 ToolResult shape.
- **REQ mapping**: REQ-AGENT-TOOL-CATALOG-001
- **Dependencies**: T-001
- **Planned files**:
  - ADD: `app/agents/__init__.py` (empty marker)
  - ADD: `app/agents/tool_registry.py` (~250 LOC: 7 args TypedDict + 7 result TypedDict + REGISTRY dict + dispatcher signature)
- **ANALYZE**: SPEC §Tool Registry Catalog 의 7 tool args/result schema 를 TypedDict 로 변환. `Literal` 사용 (axis enum, source enum, action enum). `_to_jsonable` cascade 재사용 (SPEC-MEMORY-001 패턴 — `app/channels/_jsonable.py`).
- **IMPROVE**: REGISTRY = `dict[str, ToolMetadata]` (name → {description, args_typeddict, result_typeddict, dispatch_fn_path, langfuse_span_tag, side_effect_doc}). `description` 은 LLM 이 보는 텍스트.
- **Acceptance criteria**: 7개 tool 이름 enum 확정. `python -c "from app.agents.tool_registry import REGISTRY; assert len(REGISTRY)==7"` 통과. 모든 TypedDict import-able 없는 circular.

### Task 3a — `analyze_image` tool wrapper

- **ID**: T-003a
- **Description**: `app.channels.vision.extract_vision_v2` 의 thin async wrapper.
- **REQ mapping**: REQ-AGENT-TOOL-WRAPPING-001, REQ-AGENT-SEC-URL-001
- **Dependencies**: T-002
- **Planned files**:
  - ADD: `app/agents/tools/__init__.py`
  - ADD: `app/agents/tools/analyze_image.py` (~50 LOC, args validate → SSRF guard → extract_vision_v2 → result shape)
  - ADD: `tests/test_agent_v2/test_tool_registry.py` (이 task 에서 `analyze_image` 부분만)
- **ANALYZE**: `app/channels/vision.py::extract_vision_v2` signature 확인. `app/models/request.py::image_url validator` (SSRF guard) 위치 확인.
- **IMPROVE**: SSRF 검증 → `extract_vision_v2` 호출 → VisionResult 를 LLM-consumable dict 로 shape (style_node_primary 등 7 키). Exception 시 `ToolResult(error=str(exc)[:500], result_summary=None)`.
- **Acceptance criteria**: happy-path test + SSRF-rejection test 2개 green. 함수 80 LOC 미만 (REQ-AGENT-TOOL-WRAPPING-001 AST guard).

### Task 3b — `search_products` tool wrapper

- **ID**: T-003b
- **REQ**: REQ-AGENT-TOOL-WRAPPING-001
- **Deps**: T-002
- **Files**: ADD `app/agents/tools/search_products.py`; UPDATE `tests/test_agent_v2/test_tool_registry.py` (+1 happy + 1 error).
- **ANALYZE**: `app.pipeline.runner.run_pipeline` signature 확인. `PipelineState` shape.
- **IMPROVE**: args (query + filters + top_k) → PipelineState 조립 → `run_pipeline` → candidates list 를 LLM-consumable dict 로 shape (product_id, brand, title, price, image_url, rrf_score).
- **AC**: happy + RPC-fail test green. 80 LOC 미만.

### Task 3c — `refine_search` tool wrapper (evaluator fold)

- **ID**: T-003c
- **REQ**: REQ-AGENT-TOOL-WRAPPING-001, OQ-7 (α 채택)
- **Deps**: T-003b
- **Files**: ADD `app/agents/tools/refine_search.py`; UPDATE test_tool_registry.py.
- **ANALYZE**: 현 `app/graphs/nodes/critique_apply.py` 의 body 분석. CritiqueDelta → search re-query 의 데이터 흐름. `app/graphs/nodes/evaluator.py` 의 점수/delta 생성 로직 분리.
- **IMPROVE**: args (delta) + 이전 context (state.candidates, state.image_url, state.vision_result — agent_loop 가 주입) → critique_apply logic → search re-query. **OQ-7 α: 내부에 evaluator 호출 fold 안 함** (단순화) — refine_search 는 1회 retry only. LLM 이 다시 refine_search 호출하려면 next iteration 으로 책임.
- **AC**: critique_apply 의 v1 regression test (baseline scenario #3 "더 저렴한 거") 가 동일 candidates 반환.

### Task 3d — `update_taste` tool wrapper

- **ID**: T-003d
- **REQ**: REQ-AGENT-TOOL-WRAPPING-001
- **Deps**: T-002
- **Files**: ADD `app/agents/tools/update_taste.py`; UPDATE test_tool_registry.py.
- **ANALYZE**: `app.channels.taste_profile_pg.update` signature. SPEC-MEMORY-001 Protocol 호환.
- **IMPROVE**: args (source, brand_likes, brand_dislikes, keyword_likes, keyword_dislikes) → TasteProfile.update() 호출. Source enum 검증.
- **AC**: happy (TasteProfile mutation 확인) + invalid-source-enum-rejection test.

### Task 3e — `ask_user_clarification` tool wrapper

- **ID**: T-003e
- **REQ**: REQ-AGENT-TOOL-WRAPPING-001
- **Deps**: T-002
- **Files**: ADD `app/agents/tools/ask_user_clarification.py`; UPDATE test_tool_registry.py.
- **ANALYZE**: `app/channels/clarify.py::build_card` + `TelegramAdapter.sendMessage` 호출 패턴 (apply_clarify.py + ask_clarify.py 코드 참고).
- **IMPROVE**: args (axis, options, prompt) → build_card → adapter.sendMessage with InlineKeyboard. Side effect = card sent. Result = {card_sent: bool}. **SEMANTICS**: tool 이 return 후 agent loop 는 반드시 다음 iteration 에서 `respond` 호출하여 자연어 prompt 전송 (이미 sendMessage 했지만 자연어 prompt 추가) — system prompt 에 명시.
- **AC**: card sent + correct callback_data (`clarify:{axis}:{value}`).

### Task 3f — `get_recent_history` tool wrapper

- **ID**: T-003f
- **REQ**: REQ-AGENT-TOOL-WRAPPING-001, OQ-8
- **Deps**: T-002, T-000 (need post-amendment conv_log schema)
- **Files**: ADD `app/agents/tools/get_recent_history.py`; UPDATE test_tool_registry.py.
- **ANALYZE**: `ai.log_conversation_event` SELECT 패턴 — `app/observability/conversation_log.py` 의 INSERT 와 대칭. idx_log_conv_user_time 인덱스 사용.
- **IMPROVE**: args (n ≤ 20, event_types optional filter) → SQL SELECT (use existing connection pool) → events list → **OQ-8 의 per-event-type whitelist 적용**하여 `payload_summary` 생성.
- **AC**: 5 event seed → SELECT → whitelist 적용된 결과. Backend in_memory 모드 시 empty list (fail-soft).

### Task 3g — `respond` tool wrapper

- **ID**: T-003g
- **REQ**: REQ-AGENT-TOOL-WRAPPING-001, OQ-9
- **Deps**: T-003b
- **Files**: ADD `app/agents/tools/respond.py`; UPDATE test_tool_registry.py.
- **ANALYZE**: 현 `app/graphs/nodes/respond.py` 의 ChatOpenAI 호출 패턴. `send_results` 의 카드 발송 패턴.
- **IMPROVE**: args (text, cards optional) → optional 카드 발송 (`send_results` 로직 호출) → adapter.sendMessage(text). **`_Flow` enum 제거** — 단일 open-ended ChatOpenAI prompt + kiko persona system prompt. KO/EN 분기는 session_lang 기준.
- **AC**: text + cards 모두 전송. agent_status="done" 으로 설정. `_Flow` enum import 0.

### Task 4 — `react_loop.py` core engine

- **ID**: T-004
- **REQ**: REQ-AGENT-LOOP-ITERATION-001, REQ-AGENT-LOOP-TERMINATION-001, REQ-AGENT-LOOP-EXHAUSTION-001, REQ-AGENT-FAILURE-TOOL-001, REQ-AGENT-FAILURE-LLM-JSON-001, REQ-AGENT-FAILURE-INFINITE-001, REQ-AGENT-PERF-TURN-BUDGET-001, REQ-AGENT-TOOL-DISPATCH-001
- **Dependencies**: T-001, T-002, T-003a..g
- **Planned files**:
  - ADD: `app/agents/react_loop.py` (~300 LOC core)
  - ADD: `app/agents/llm_client.py` (~60 LOC, ChatOpenAI wrapper with bind_tools)
- **ANALYZE**: SPEC §Architecture Snapshot 의 ReAct pseudocode. `ChatOpenAI.bind_tools(tools=[...])` + `ainvoke()` 의 OpenAI Tools API 반환 shape (langchain_core.messages.AIMessage.tool_calls).
- **IMPROVE**: 핵심 함수 `run_react_loop(state: WorkingState, sess: Session) -> WorkingState`:
  - For iter in range(AGENT_MAX_ITERATIONS):
    1. Build context (state + tool_call_history truncated per OQ-10, system prompt with kiko persona + tool definitions auto-discovered from REGISTRY).
    2. Pre-iter guards: **infinite-loop guard** (last 3 history entries identical → exhaustion), **token budget check** (cumulative exceeded → exhaustion).
    3. `await llm_client.ainvoke(...)` with per-LLM-call timeout `AGENT_LLM_TIMEOUT_S` (5s).
    4. JSON malformation: retry once with corrective system msg; 2회 연속 실패 → exhaustion.
    5. Validate args against TypedDict (REQ-AGENT-TOOL-DISPATCH-001). Invalid → record error in history, continue loop.
    6. **Dispatch tool** with per-tool timeout `AGENT_TOOL_TIMEOUT_S` (5s). Exception → record error, continue.
    7. Append to history + emit `tool_call` event (REQ-AGENT-OBS-001).
    8. If tool == `respond`: set agent_status="done", break.
  - Post-loop: if status != "done" → exhaustion path → fallback respond (KO/EN aware).
- **Acceptance criteria**: 7 unit tests in `test_agent_loop.py` (iteration cap, termination, exhaustion, tool exception, JSON malformation × 2, infinite-loop guard) all green.

### Task 5 — `nodes/agent.py` graph node

- **ID**: T-005
- **REQ**: REQ-AGENT-LOOP-ENTRY-001
- **Dependencies**: T-004
- **Planned files**:
  - ADD: `app/graphs/nodes/agent.py` (~80 LOC: graph node wrapper around react_loop)
- **ANALYZE**: 다른 graph node 패턴 — `@observe(name="node.agent", as_type="span")` decorator, `dict` return value, Session 접근 (`get_store().get_or_create(state.chat_id)`).
- **IMPROVE**: `async def agent(state: WorkingState) -> dict`:
  - `sess = get_store().get_or_create(state.chat_id)`
  - `result_state = await run_react_loop(state, sess)`
  - Return state delta dict (`{"agent_iterations": ..., "tool_call_history": ..., "agent_status": ..., "response_text": ..., "log_events": [...]}`).
- **AC**: graph node 호출 시 react_loop 가 invoke 됨. delta dict 가 LangGraph 의 state merge 와 호환.

> **[Topology-integration redesign — 2026-05-15]**: T-006/T-007 의 최초 설계는 V2 토폴로지(노드/엣지)만 분기했고 **`ingest` 노드 본문 안의 V1 잔재를 게이팅하지 않았다**. 런타임 검증(실 Telegram 로그, `AGENT_V2_REACT_ENABLED=true`, `AGENT_LLM_MODEL=nova-lite`)에서 4개 결함 확인:
> 1. `route_text` LLM 호출이 V2 에서도 매 텍스트 턴 실행 → `TimeoutError → fallback` + 3s 낭비 + V1 `sess.state` 오염.
> 2. contentless empty Update 가 `_route_after_ingest_v2` 의 fallthrough 로 `agent` 진입 → 빈 컨텍스트로 환각 응답.
> 3. `remember_lang` 이 Pinterest URL-only 입력을 EN 신호로 오판 → 한국어 사용자에게 영어 응답.
> 4. V1 state-machine(`awaiting_intent`)이 V2 에서도 routing 을 구동.
>
> T-006/T-007 을 아래로 **재설계**한다. Task 7.5(lang URL guard) + Task 15(topology-integration redesign) 신설. 기존 OQ resolution 은 모두 유효 — 단 OQ-4(post-onboarding → agent immediately)의 구현이 ingest 본문 게이팅 누락으로 미완이었음을 §15 가 보강한다. 어떤 OQ 도 무효화되지 않는다.

### Task 6 — `fashion_bot.py` topology edit + `routing.py` onboarding gate (재설계됨)

- **ID**: T-006
- **REQ**: REQ-AGENT-TOPOLOGY-GATE-001, REQ-AGENT-TOPOLOGY-SUPERSEDE-001, REQ-AGENT-COMPAT-FLAG-001
- **Dependencies**: T-005
- **Status**: implemented (Wave 1-7) — §15 가 런타임 결함 #2/#4 의 보강분만 추가. 토폴로지 골격은 변경 없음.
- **Planned files**:
  - MODIFY: `app/graphs/fashion_bot.py` (`_build_graph_v2` 내 `_route_after_ingest_v2` 에 **empty-input END 가드** 추가 — §15 Decision 2)
  - MODIFY: `app/graphs/routing.py` (V1 분기 무변경. V2 routing fns 는 `fashion_bot.py` 내 클로저로 유지)
- **ANALYZE (PRESERVE)**: 현 `fashion_bot.py` 의 V1 21 nodes + 9 conditional edges. Onboarding 6 노드 + `_route_after_onboard_fit` 는 절대 건드리지 않음 — characterization test 로 보호. `_build_graph_v2()` 의 노드/엣지 골격(이미 구현됨)은 정상 — 결함은 routing 클로저의 **fallthrough 정책**뿐.
- **PRESERVE**: `test_v1_topology_unchanged` (flag=false byte-identical), `test_onboarding_subgraph_unchanged`, pick→tap→agent happy-path characterization (아래 §15 Decision 3 의 검증 결과를 회귀 테스트로 고정).
- **IMPROVE (§15 보강분만)**:
  - `_route_after_ingest_v2` 의 마지막 `return "agent"` 직전에 **empty-input 가드** 삽입 (정확한 게이트 조건은 §15 Decision 2). contentless Update → `"__end__"` (silent).
  - `ingest_branches_v2` 에 `"__end__": END` 추가 (가드의 반환값 매핑).
  - 그 외 V2 토폴로지(노드 add_node 목록, deprecated 노드 미등록, `resolve_image→vision_node→agent`, `pick_item→agent|END`, `apply_clarify→agent`, `agent→END`)는 **변경 없음 — 이미 정확함**.
- **AC**:
  - flag=true 시 `GRAPH.get_graph().nodes` 에 deprecated 5개 노드명(`router_text`/`critique_apply`/`taste_update`/`respond`/`evaluator`) 부재 + `agent` 노드 존재.
  - flag=false 시 V1 토폴로지 byte-identical.
  - onboarding 6-combination test green.
  - **신규**: contentless Update(text 공백 AND no callback AND no urls AND no photo, onboarding 아님) → 그래프가 어떤 adapter send 도 호출하지 않고 END (§15 AC-2).
  - **신규**: pick→tap→agent characterization test green (selected_item_index 세팅 → agent 진입 확인).

### Task 7 — `ingest.py` V1-잔재 게이팅 + Step C inline clarify (재설계됨)

- **ID**: T-007
- **REQ**: REQ-AGENT-TOPOLOGY-GATE-001 (mid-onboarding clarify negative path), REQ-AGENT-TOPOLOGY-SUPERSEDE-001 (V2 에서 V1 router 경로 비활성), REQ-AGENT-COMPAT-FLAG-001
- **Dependencies**: T-006
- **Status**: Step C 는 구현됨. **route_text 게이팅이 누락 → §15 Decision 1 로 보강.**
- **Planned files**:
  - MODIFY: `app/graphs/nodes/ingest.py` (route_text 블록을 V2 에서 skip — §15 Decision 1)
- **ANALYZE (PRESERVE)**: `ingest.py` 의 Step A(implicit feedback lazy attribution, lines 79-90) + Step B(없음 — thread_id 는 webhook intake 가 InputState 로 주입, ingest 는 `turn_no:1` 만 반환) + Step C(inline clarify, lines 92-135, 이미 구현). `needs_router`/`route_text` 블록(lines 137-167)이 결함 #1 의 원천.
- **PRESERVE**:
  - `test_ingest_step_a_unchanged`: implicit feedback Step A 가 route_text 게이팅과 무관하게 동작(Step A 는 route_text 블록보다 먼저 실행 — 순서 보존 필수).
  - `test_ingest_step_c_unchanged`: clarify:* inline 누적 + mid-onboarding node_error 동작 무변경.
  - `test_ingest_v1_router_path`: flag=false 시 `route_text` 호출 + `decision` 반환 무변경.
- **IMPROVE (§15 Decision 1)**:
  - `needs_router` 계산 직전에 V2 가드 추가: V2 활성(flag=true AND `AGENT_LLM_MODEL` set)이면 `needs_router=False` 강제 → `route_text` 절대 미호출, `_emit_intent_routed(state, None)` 후 `{"log_events":..., "turn_no":1}` 즉시 반환. `decision` 은 None 으로 남음(V2 에서 dead — 어떤 V2 routing/agent 코드도 `state.decision` 미참조, §15 Decision 4 의 consumer 감사로 확인).
  - Step A/Step C 는 V2 가드보다 **먼저** 실행되므로 무영향.
- **AC**:
  - V2 flag=true 시 `route_text` 호출 0회(mock 으로 assert_not_called) — 결함 #1 해소.
  - `intent_routed` 이벤트는 여전히 emit(decision=None).
  - flag=false 시 route_text 호출 + decision 반환 byte-identical.
  - Step C clarify 누적 + mid-onboarding node_error 회귀 0.

### Task 7.5 — `lang.py::remember_lang` URL-only guard (신설)

- **ID**: T-007.5
- **REQ**: REQ-AGENT-COMPAT-SEMANTIC-001 (sticky-lang 보존), SPEC-MSG-001 sticky-language 계약 보강
- **Dependencies**: 없음 (parallel-startable, V2 토폴로지와 직교)
- **근거**: pre-existing SPEC-MSG-001 버그지만 V2 UX 를 사용 불가로 만들어(한국어 사용자가 Pinterest URL 던지면 이후 전 응답 영어) 본 redesign 에 fold. `remember_lang` 은 `/`-command 와 <3-char non-Hangul 만 sticky-preserve 하고 **URL-only 입력은 미가드** → "https://pin.it/abc"(24자, no Hangul) → `detect_lang`=en → sticky 가 en 으로 flip.
- **Planned files**:
  - MODIFY: `app/channels/lang.py` (`remember_lang` 에 URL-only guard 1 절 추가)
  - ADD: `tests/test_agent_v2/` 또는 기존 lang 테스트에 4 케이스
- **ANALYZE**: `remember_lang` (lang.py:37-63) 의 guard 순서: empty → `/`-command → <3-char non-Hangul → `detect_lang`. URL-only guard 를 `/`-command guard 와 동형으로(early-return prior) 추가.
- **IMPROVE (§15 Decision 5 — 정확한 게이트)**:
  - `stripped` 의 모든 whitespace-delimited token 이 URL-like(`scheme://...` 또는 `pin.it/...` 또는 `www.` prefix host)이면 → 언어 신호 아님 → `return prior` (sticky 보존).
  - 혼합 입력("이거 찾아줘 https://pin.it/x")은 비-URL token("이거","찾아줘") 존재 → 가드 미통과 → 기존대로 `detect_lang`(Hangul 有 → ko). **정상 KO/EN 회귀 0**.
- **AC**:
  - "https://pin.it/abc123" (prior=ko) → 반환 "ko", `sess.lang` 무변경 (URL-only).
  - "https://www.pinterest.com/board/" (prior=en) → "en" 보존.
  - "이거랑 비슷한 거 https://pin.it/x" → "ko" (혼합 — Hangul 검출 정상).
  - "casual blazer" → "en" (일반 텍스트 회귀 0).
  - "캐주얼 블레이저" → "ko" (일반 텍스트 회귀 0).

### Task 8 — Feature flag + `/health/ready` 노출

- **ID**: T-008
- **REQ**: REQ-AGENT-COMPAT-FLAG-001
- **Dependencies**: T-006
- **Planned files**:
  - MODIFY: `app/core/config.py` (add 6 env vars)
  - MODIFY: `app/api/health.py` (/health/ready 에 agent_v2_react_enabled 필드 추가)
  - ADD: 1 test in `tests/test_agent_v2/test_topology.py` (health endpoint reflects flag)
- **ANALYZE**: `app/core/config.py` 의 settings 패턴 (pydantic BaseSettings). `/health/ready` 의 현 response shape.
- **IMPROVE**: 6 env vars 추가 (SPEC §Environment Variables 표 그대로): `AGENT_V2_REACT_ENABLED`, `AGENT_MAX_ITERATIONS=6`, `AGENT_TURN_TOKEN_BUDGET=32000`, `AGENT_TOOL_TIMEOUT_S=5`, `AGENT_LLM_MODEL` (no default — fail-closed), `AGENT_LLM_TIMEOUT_S=5`. `/health/ready` JSON 에 `"agent_v2_react_enabled": bool, "agent_llm_model_configured": bool` 추가.
- **AC**: ENV 변경 시 settings 재로드 → /health/ready 반영. `AGENT_LLM_MODEL` 미설정 시 effective flag = false (fail-closed unit test).

### Task 9 — 8 new test files in `tests/test_agent_v2/`

- **ID**: T-009
- **REQ**: 모든 P0 REQs 의 acceptance test backing
- **Dependencies**: T-001..T-008 모두
- **Planned files**:
  - ADD: `tests/test_agent_v2/__init__.py`
  - ADD: `tests/test_agent_v2/test_agent_loop.py` (~300 LOC)
  - ADD: `tests/test_agent_v2/test_tool_registry.py` (~250 LOC, 7 tools × 2 path = 14 base tests)
  - ADD: `tests/test_agent_v2/test_topology.py` (~200 LOC, gate 6-combo + flag toggle + deprecated-unreachable)
  - ADD: `tests/test_agent_v2/test_backward_compat.py` (~250 LOC, 6 baseline scenarios)
  - ADD: `tests/test_agent_v2/test_tool_call_logging.py` (~150 LOC, Langfuse span + conv_log row twin assertion)
  - ADD: `tests/test_agent_v2/test_failure_modes.py` (~200 LOC, 7 tool exception + JSON malformation × 2 + infinite-loop guard + SSRF + token budget)
  - ADD: `tests/test_agent_v2/test_performance.py` (~100 LOC, mocked latencies + load test scaffold)
  - ADD: `tests/test_agent_v2/test_security.py` (~150 LOC, SSRF + AST scan no-eval + payload truncation)
- **ANALYZE**: 기존 `tests/` 구조 확인 — pytest_asyncio fixtures, mock LLM 패턴. SPEC §Test Plan Outline 의 8 파일 명세 그대로.
- **IMPROVE**: 각 파일에 acceptance.md 의 test_id × test_function 매핑 1:1 구현.
- **AC**: `pytest tests/test_agent_v2/ -q` green. `pytest --cov=app.agents` ≥ 85%.

### Task 10 — V1 regression test migration

- **ID**: T-010
- **REQ**: REQ-AGENT-COMPAT-SEMANTIC-001
- **Dependencies**: T-009
- **Planned files**:
  - MODIFY: ~50 tests in `tests/test_graph_flows.py` (현 600+ 중 routing edge-case 100여개)
  - DELETE: router_text classification tests (~10개, agentic LLM 의 자율 결정으로 대체)
- **ANALYZE**: 현 tests 중 routing-specific 테스트 식별 (router_text mock 사용 + 4-way enum 분기 assert).
- **PRESERVE**: 출력 클래스 보존 — wording-level assertion 을 property-based 로 변환 (e.g., `assert "card_carousel" in adapter.sent_messages` instead of `assert text == "exact V1 wording"`).
- **IMPROVE**: deprecated 노드의 internal logic 테스트는 삭제 (tool wrapper test 가 대체).
- **AC**: `pytest tests/` (전체) under `AGENT_V2_REACT_ENABLED=true` and `=false` 모두 green.

### Task 11 — Per-tool observability SQL queries + operator runbook

- **ID**: T-011
- **REQ**: REQ-AGENT-OBS-METRICS-001
- **Dependencies**: T-009
- **Planned files**:
  - ADD: `docs/runbooks/agent-v2-observability.md` (~150 lines, SQL queries + Langfuse dashboard URL templates + per-tool selection distribution + cost monitoring)
- **IMPROVE**: SPEC §REQ-AGENT-OBS-METRICS-001 의 2개 SQL + 추가 4개:
  - Per-tool latency p50/p95 (catalog 의 query 1)
  - Per-turn iteration distribution (catalog 의 query 2)
  - Per-tool selection distribution (R10 mitigation)
  - Exhaustion rate per day
  - Token budget breach rate
  - Tool error rate per tool_name
- **AC**: 6개 query 모두 indexed scan 사용 (EXPLAIN 검증).

### Task 12 — Load test scaffolding

- **ID**: T-012
- **REQ**: REQ-AGENT-PERF-HAPPY-001, REQ-AGENT-PERF-EXHAUST-001
- **Dependencies**: T-009
- **Planned files**:
  - ADD: `scripts/load_test_agent_v2.py` (~200 LOC, locust 또는 asyncio-based)
  - ADD: `docs/runbooks/agent-v2-perf-test.md`
- **IMPROVE**: 200-turn happy + 50-turn exhaustion. 측정: p50/p95/p99 end-to-end webhook→bot_message latency.
- **AC**: dev 환경에서 happy p95 < 8s, exhaust p95 < 12s 달성.

### Task 13 — Cutover runbook (dev → prod flip)

- **ID**: T-013
- **REQ**: REQ-AGENT-COMPAT-FLAG-001 운영 측면
- **Dependencies**: T-011, T-012
- **Planned files**:
  - ADD: `docs/runbooks/agent-v2-cutover.md` (~200 lines)
- **IMPROVE**: 4-phase runbook (OQ-5 의 cutover 시퀀스):
  1. Dev `true` set + 24h burn-in (모니터링 항목: tool selection 분포, latency p95, exhaustion rate, cost/day).
  2. Prod manual smoke (DoD §8 의 8개 시나리오 (a)-(h)).
  3. Prod flag flip in low-traffic window (KST 03:00-05:00).
  4. 24h prod monitor + revert triggers (latency p95 > 15s, exhaustion rate > 10%, cost > $100/day, error rate > 5%).
- **AC**: Runbook 검토 완료 + revert procedure 명시.

### Task 14 — Deprecated nodes docstring + V2.1 cleanup SPEC stub

- **ID**: T-014
- **REQ**: REQ-AGENT-TOPOLOGY-SUPERSEDE-001 의 docstring 부분, V2.1 cleanup planning
- **Dependencies**: T-006
- **Planned files**:
  - MODIFY: `app/graphs/nodes/critique_apply.py` (module docstring +DEPRECATED notice referencing SPEC-AGENT-V2-REACT)
  - MODIFY: `app/graphs/nodes/taste_update.py` (동일)
  - MODIFY: `app/graphs/nodes/respond.py` (동일)
  - MODIFY: `app/graphs/nodes/evaluator.py` (OQ-7 fold 결정 이후 deprecated)
  - MODIFY: `app/channels/router.py` (deprecated when flag=true; retained for flag=false). docstring 명시.
  - ADD: `.moai/specs/SPEC-AGENT-V2-CLEANUP-001/spec.md` (stub — V2.1 cleanup SPEC for 5개 deprecated 모듈 + `_router_text_passthrough` 제거)
- **IMPROVE**: 각 deprecated 모듈 top docstring 에:
  ```
  DEPRECATED — superseded by SPEC-AGENT-V2-REACT (agent loop + tool registry).
  Retained for V2.0 rollback safety only. Will be removed in V2.1 cleanup
  (see SPEC-AGENT-V2-CLEANUP-001).
  ```
- **AC**: 5개 모듈 docstring에 "DEPRECATED" + SPEC ID 문자열 존재. V2.1 cleanup SPEC stub 생성 (P3, status=stub).

### Task 15 — Topology-integration redesign (런타임 결함 4종 보강)

- **ID**: T-015
- **REQ**: REQ-AGENT-TOPOLOGY-SUPERSEDE-001(V1 router 비활성), REQ-AGENT-TOPOLOGY-GATE-001, REQ-AGENT-COMPAT-SEMANTIC-001(sticky-lang)
- **Dependencies**: T-006, T-007 (이미 구현된 V2 토폴로지 위에 보강)
- **Methodology**: DDD ANALYZE-PRESERVE-IMPROVE (4개 brownfield edit, 모두 additive/게이팅)
- **목적**: V2 토폴로지 골격은 정상이나 `ingest`/`lang` 본문의 V1 잔재가 런타임에서 충돌. 4개 게이팅/가드만 추가, 신규 추상화 0.

#### 결함 → 결정 매핑

| # | 런타임 증상 | 원천 파일·라인 | 결정 |
|---|---|---|---|
| 1 | 매 텍스트 턴 `route_text` LLM TimeoutError fallback (V2 에서도) | `ingest.py:137-167` `needs_router`/`route_text` | Decision 1 |
| 2 | contentless empty Update → agent 환각 응답 | `fashion_bot.py:218-232` `_route_after_ingest_v2` fallthrough | Decision 2 |
| 3 | pick→tap→agent 가 실제로 끊기는가? | `fashion_bot.py:221,247` + `pick_item.py:121-181` | Decision 3 (검증결과: 안 끊김) |
| 4 | `awaiting_intent` V1 state-machine 이 V2 routing 구동 | `Session.state` consumers | Decision 4 |
| 5 | Pinterest URL-only → sticky-lang en flip | `lang.py:37-63` `remember_lang` | Decision 5 |

#### Decision 1 — V2 에서 `route_text` 완전 skip

**결정**: V2 활성 시 `ingest` 는 `route_text` 를 **절대 호출하지 않는다**. `decision` 은 None 으로 남기며 이는 V2 에서 완전 dead 다 (consumer 감사: `_route_after_ingest_v2`/`_route_after_pick_v2`/`_route_after_vision_v2`/`agent.py`/`react_loop.py` 중 `state.decision` 참조 0).

**게이트(pseudocode-level)** — `ingest.py` 의 `needs_router` 계산 직전:

```
v2_active = settings.AGENT_V2_REACT_ENABLED and (settings.AGENT_LLM_MODEL or "").strip()
if v2_active:
    _emit_intent_routed(state, None)          # intent_routed 이벤트 계약 유지 (decision=None)
    return {"log_events": breadcrumbs, "turn_no": 1}   # route_text 미진입
# (이하 기존 needs_router / route_text 블록은 V1 전용으로 유지)
```

순서 불변식: 이 early-return 은 **Step A(implicit feedback) + Step C(clarify inline) 이후, `needs_router` 이전**에 위치. Step A/C 의 부수효과(re-query, boost_keywords 누적)는 보존된다.

**`sess.state` 미설정 안전성**: V2 에서 `route_text`/`_route_after_router_text` 미사용 → `decision` dead. `sess.state` 는 Decision 4 의 consumer 감사로 V2 안전 확인.

#### Decision 2 — empty/contentless input 가드 (위치: `_route_after_ingest_v2`, 정책: silent END)

**"no actionable input" 정의 (정밀)**: onboarding gate + continuous-pinterest + `cb.startswith("item:")` + photo/url + `AWAITING_ITEM_PICK`+digit-text 가 **모두 미적중**이고, 그리고:

```
(state.message.text or "").strip() == ""
AND not state.message.callback_data            # clarify:/crit:/onboard: 콜백은 cb 非공백 → 가드 미통과 (정상 agent 행)
AND not state.message.urls
AND not state.message.photo_file_id
```

**위치 결정 = `_route_after_ingest_v2` 가 `"__end__"` 반환** (대안 평가):

| 위치 | 장점 | 단점 | 채택 |
|---|---|---|---|
| (a) routing END | agent 미스폰, LLM 비용 0, 환각 원천 차단, 단일 funnel, routing 단위테스트로 검증 | 없음 (콜백/실입력은 이 지점 이전에 분기 완료) | ✅ |
| (b) agent 노드 early-return | — | agent 스폰 오버헤드, 가드 로직 중복, routing 결정을 노드가 떠안음 | ✗ |
| (c) react_loop context-builder | — | LLM 이미 호출되는 계층(환각 발생점), 엔진에 no-op 특수분기 오염 | ✗ |

**정당화**: "agent 가 돌아야 하는가"는 routing 계층의 책임. `_route_after_ingest_v2` 는 V2 의 단일 funnel 이며 END 는 무비용·무발화.

**사용자가 보는 것 = 아무것도 없음 (nudge 금지)**. 근거: Telegram 은 service message / sticker / 빈 텍스트 echo 등 contentless Update 를 spuriously 발생시킨다. 매번 nudge 하면 그게 곧 보고된 UX 버그("over-responding is the bug"). contentless Update 는 사용자 턴이 아니므로 침묵이 정답.

**구현 매핑**: `_route_after_ingest_v2` 마지막 `return "agent"` 직전에 위 조건 → `return "__end__"`; `ingest_branches_v2` 에 `"__end__": END` 추가.

#### Decision 3 — pick-callback 조사 결론 (DEFINITIVE)

**"empty webhook 이 사실 drop 된 pick tap 인가?" → 아니오 (NO).**

증거 체인:
1. `adapter.py:292-311` — `callback_query` 페이로드 → `callback_data=str(cbq.get("data"))` (탭 = `"item:N"`, 비어있지 않음, well-formed ChannelMessage).
2. `fashion_bot.py:221` — `if cb.startswith("item:"): return "pick_item"` (탭 콜백 인식, drop 아님).
3. `pick_item.py:148-181` — `item:N` 경로가 `sess.selected_item_index=idx` + `state` 반환 (`{"selected_item_index": idx, ...}`).
4. `fashion_bot.py:247` — `_route_after_pick_v2`: `return "agent" if state.selected_item_index is not None else "__end__"` → 탭 후 selected_item_index 세팅됨 → **agent 진입 (정상)**.

**결론**: pick→tap→agent 체인은 V2 에서 **정확히 배선되어 있다**. "empty webhook"(text='' photo=False urls=[] callback empty)은 drop 된 탭이 아니라, **별개의 진짜 contentless Telegram Update** (service msg / sticker / 공백 텍스트 메시지 — `parse_inbound:326` 의 `text = message.get("text") or message.get("caption")` 가 None 이 되어도 ChannelMessage 검증 통과 → `_route_after_ingest_v2` fallthrough 로 `agent` 진입). 따라서 #2 의 진짜 원인 = Decision 2 의 누락된 empty-input 가드. **pick 경로는 무수정.**

#### Decision 4 — `Session.state` consumer 감사 (V1-only vs shared)

`SessionState` enum: IDLE / LINK_RESOLUTION / AWAITING_IMAGE_PICK / VISION_PROCESSING / AWAITING_ITEM_PICK / AWAITING_CLARIFY / AWAITING_INTENT / SEARCHING / RESULTS_SENT.

| Consumer | 읽는 state | 분류 | V2 동작 |
|---|---|---|---|
| `ingest.py:143` `needs_router` | RESULTS_SENT/IDLE/AWAITING_INTENT | **V1-only** | Decision 1 으로 V2 미실행 — 무해 |
| `routing.py:260` `_route_after_ingest`(V1) | AWAITING_INTENT/RESULTS_SENT/IDLE → router_text | **V1-only** | V2 는 `_route_after_ingest_v2` 사용 — 미참조 |
| `_route_after_ingest_v2` (fashion_bot.py:227) | AWAITING_ITEM_PICK (digit-pick fallback) | **shared** | `pick_item` 이 AWAITING_ITEM_PICK set (pick_item.py:198) — V2 정상 동작 |
| `implicit_feedback.detect_and_apply_re_query:439` | `state == RESULTS_SENT` | **shared** | V2 엔 send_results 노드 없음 → RESULTS_SENT setter 부재. **알려진 V2 한계** (아래) |
| onboarding(`onboarding_required` 등) | `onboard_stage`/`onboarded_at` (state 아님) | 무관 | 영향 0 |
| conv_log emit | `thread_id`/`turn_no` (state 아님) | 무관 | 영향 0 |

**결정**: V2 에서 `sess.state` 를 `IDLE`/미설정으로 두어도 onboarding·conv_log·agent·pick 분기 무손상. `awaiting_intent` 가 V2 routing 을 구동하던 경로(`_route_after_ingest` V1)는 V2 에서 이미 미사용 — Decision 1 으로 `ingest` 본문의 마지막 V1 잔재(route_text)까지 제거되어 OQ-4(post-onboarding→agent immediately) 가 비로소 완성된다.

**알려진 V2 한계 (follow-up, 본 redesign 범위 외 — 결함 아님)**: V2 엔 `RESULTS_SENT` 를 set 하는 노드가 없어 `implicit_feedback` 의 RESULTS_SENT 기반 자동 re-query 가 V2 에서 트리거되지 않는다. 이는 enhancement 의 부재(crash 아님)이며, V2.1 에서 `respond`/`search_products` tool 이 `sess.state=RESULTS_SENT` 를 set 하도록 보강 예정. 본 redesign 은 환각/낭비 호출 차단에 집중하므로 scope 외로 명시.

#### Decision 5 — `remember_lang` URL-only guard

**게이트(정밀)** — 기존 `/`-command guard 와 동형으로, `len(stripped) < 3` guard 직후:

```
tokens = stripped.split()
if tokens and all(_is_url_like(t) for t in tokens):
    return prior          # URL-only/link-only → 언어 신호 아님, sticky 보존
# _is_url_like(t): t.startswith(("http://","https://")) or t.startswith("www.")
#                  or re.match(r"^(?:[\w-]+\.)*pin\.it/", t)  (pin.it 단축)
```

**회귀 안전성**: 혼합 입력(URL + 자연어 토큰)은 비-URL token 존재 → `all(...)` False → 가드 미통과 → 기존 `detect_lang` 경로 그대로 (Hangul 있으면 ko). 일반 텍스트는 URL token 0 → 무영향. KO/EN 정상 검출 회귀 0.

#### File-by-file 구현자 변경 목록 (코드 아님 — 무엇을 할지)

| 파일 | 변경 | 결정 |
|---|---|---|
| `app/graphs/nodes/ingest.py` | `needs_router` 계산 직전(Step C 이후, line ~137)에 `v2_active` early-return 절 추가: `_emit_intent_routed(state, None)` 후 `{"log_events":..., "turn_no":1}` 반환. 기존 needs_router/route_text/except 블록은 V1 전용으로 그대로 남김(들여쓰기 무변경). | 1 |
| `app/graphs/fashion_bot.py` | `_route_after_ingest_v2` 의 `return "agent"` 직전에 empty-input 조건(Decision 2 정의) → `return "__end__"`. `ingest_branches_v2` dict 에 `"__end__": END` 항목 추가. 그 외 V2 토폴로지 무변경. | 2 |
| `app/channels/lang.py` | `remember_lang` 에 URL-only guard 1 절(<3-char guard 직후, Decision 5 게이트). 모듈 top 에 URL 패턴 정규식 1개 추가 가능. `detect_lang`/`session_lang` 무변경. | 5 |
| `pick_item.py` / `_route_after_pick_v2` | **변경 없음** (Decision 3: 경로 정상 — 회귀 테스트만 추가). | 3 |

신규 추상화·신규 모듈 0. 4개 파일 중 실변경 3개(ingest/fashion_bot/lang), 1개(pick)는 무변경 회귀고정.

#### Acceptance criteria (fix 별)

- **AC-1 (route_text skip)**: `AGENT_V2_REACT_ENABLED=true` + `AGENT_LLM_MODEL` set 에서 텍스트 턴 처리 시 `app.channels.router.route_text` mock 이 **호출 0회**. `intent_routed` 이벤트는 1회 emit(payload.intent="unknown", decision None). flag=false 시 route_text 1회 호출 + `decision` 반환 byte-identical.
- **AC-2 (empty-input silent END)**: contentless ChannelMessage(text 공백, callback None, urls [], photo None, onboarding 아님) → 그래프 ainvoke 후 adapter 의 sendMessage/sendPhoto **호출 0회**, 그래프 terminal=END, agent 노드 미진입(span 부재). nudge 메시지 미발생.
- **AC-3 (pick happy-path 회귀)**: photo/Pinterest-URL → vision(multi) → pick_item carousel → END; 다음 턴 `item:0` 콜백 → pick_item selected_item_index=0 set → `_route_after_pick_v2`="agent" → agent 진입. characterization test 로 고정 (Decision 3 검증 결과).
- **AC-4 (state-machine 무해)**: V2 에서 `sess.state` 가 IDLE/미설정이어도 onboarding 6-combo + conv_log thread_id + pick AWAITING_ITEM_PICK digit-fallback 회귀 0. RESULTS_SENT re-query 부재는 known-limitation 으로 문서화(테스트는 xfail/skip + 사유).
- **AC-5 (lang URL guard)**: 위 Task 7.5 AC 5 케이스 green. 기존 lang 테스트 스위트 회귀 0.

- **AC (전체)**: `pytest tests/ -q` under `AGENT_V2_REACT_ENABLED=true` AND `=false` 양쪽 green. 실 Telegram 시나리오(한국어 사용자 + Pinterest URL → vision → pick → tap → 한국어 agent 응답, 중간 contentless Update 무발화)가 결함 #1-4 모두 미재현.

---

## 3. File Plan

### NEW files (16)

| Path | Purpose | LOC est |
|---|---|---|
| `app/agents/__init__.py` | package marker | 1 |
| `app/agents/tool_registry.py` | TypedDicts + REGISTRY dispatch table | 250 |
| `app/agents/react_loop.py` | ReAct loop core | 300 |
| `app/agents/llm_client.py` | ChatOpenAI bind_tools wrapper | 60 |
| `app/agents/tools/__init__.py` | package marker | 1 |
| `app/agents/tools/analyze_image.py` | Vision v2 wrapper | 50 |
| `app/agents/tools/search_products.py` | run_pipeline wrapper | 60 |
| `app/agents/tools/refine_search.py` | CritiqueDelta + re-search | 80 |
| `app/agents/tools/update_taste.py` | TasteProfile.update wrapper | 50 |
| `app/agents/tools/ask_user_clarification.py` | clarify card + sendMessage | 60 |
| `app/agents/tools/get_recent_history.py` | conv_log SELECT + whitelist | 80 |
| `app/agents/tools/respond.py` | ChatOpenAI + send_results | 80 |
| `app/graphs/nodes/agent.py` | graph node wrapper | 80 |
| `tests/test_agent_v2/__init__.py` | package marker | 1 |
| `tests/test_agent_v2/test_{agent_loop,tool_registry,topology,backward_compat,tool_call_logging,failure_modes,performance,security,state_extension}.py` | 9 test files | ~1700 total |
| `docs/runbooks/agent-v2-{observability,perf-test,cutover}.md` | 3 runbooks | ~550 total |

### MODIFIED files (6)

| Path | Reason | Risk level |
|---|---|---|
| `app/graphs/state.py` | +3 fields to WorkingState | Low (additive) |
| `app/graphs/fashion_bot.py` | feature-flag-gated topology branch | High (core graph) |
| `app/graphs/routing.py` | new `_route_after_ingest_v2`, deprecated 4 fns | Medium |
| `app/graphs/nodes/ingest.py` | Step C (inline clarify) | Low (additive when flag=true) |
| `app/core/config.py` | +6 env vars | Low |
| `app/api/health.py` | +2 fields in /health/ready | Low |
| `app/observability/conversation_log.py` | `tool_call` event_type whitelist (Task 0 cross-SPEC) | Cross-SPEC |

### DEPRECATED files (5, retained V2.0 / removed V2.1)

| Path | Body lives in | V2.1 cleanup |
|---|---|---|
| `app/graphs/nodes/critique_apply.py` | `app/agents/tools/refine_search.py` | SPEC-AGENT-V2-CLEANUP-001 |
| `app/graphs/nodes/taste_update.py` | `app/agents/tools/update_taste.py` | 동일 |
| `app/graphs/nodes/respond.py` | `app/agents/tools/respond.py` (`_Flow` enum 제거) | 동일 |
| `app/graphs/nodes/evaluator.py` | absorbed into refine_search (OQ-7 α) | 동일 |
| `app/channels/router.py` | route_text logic 은 agent LLM 의 reasoning 으로 흡수 | 동일 + `_router_text_passthrough` 제거 |

### UNCHANGED (asserted by REQ-AGENT-TOPOLOGY-SUPERSEDE-001 + REQ-AGENT-COMPAT-STATE-001)

- 모든 `app/graphs/nodes/onboard_*.py` (6 nodes — SPEC-ONBOARD-CARDS-001 v0.3.2 freeze)
- `app/graphs/nodes/resolve_image.py`, `vision.py`, `pick_item.py`, `ask_clarify.py`, `apply_clarify.py` (subsumed into ingest Step C when V2), `search.py`, `send_results.py`, `pinterest_ingest.py`
- `app/channels/{vision,vision_prompt,clarify,clarify_values,lang,link_resolver,session,taste_profile,taste_profile_pg,implicit_feedback,onboarding_*,pinterest_url,_jsonable,_pinterest_helpers}.py`
- `app/pipeline/**`, `app/providers/**`
- `app/api/{webhooks/telegram,recommend}.py`
- `app/models/**`, `app/main.py`
- `app/observability/langfuse.py`, `event_payloads.py`

---

## 4. DDD Cycle Per Brownfield Task

Brownfield (기존 파일 수정) task 별 ANALYZE-PRESERVE-IMPROVE 세부.

### T-001 (state.py)

- **ANALYZE deliverable**: 27 기존 필드 카탈로그 (필드명 / 타입 / default / reducer 유무). `_LIST_ADD` reducer 패턴 학습.
- **PRESERVE characterization tests**:
  - `test_state_field_set_snapshot`: 새 필드 추가 전/후 `set(WorkingState.model_fields.keys())` 비교 — 27 → 30 (+3).
  - `test_state_field_defaults_preserved`: 기존 27 필드의 default 값 baseline 비교.
  - `test_state_serialization_roundtrip`: 기존 instance → model_dump_json → model_validate_json 무손실.
- **IMPROVE plan**: 3 필드 추가 (state.py line 122 직후, onboard_pin_weights 다음). `_LIST_ADD` reducer 를 `tool_call_history` 에 적용.

### T-006 (fashion_bot.py + routing.py)

- **ANALYZE deliverable**: 현 21 노드 + 9 conditional edges 의 전체 그래프 다이어그램 (state diagram). flag false 분기에서 byte-identical 유지 보장 위한 freeze line 식별.
- **PRESERVE characterization tests**:
  - `test_v1_topology_unchanged`: flag=false 일 때 `GRAPH.get_graph().nodes` + `edges` 가 SPEC pre-change 와 동일 (snapshot).
  - `test_onboarding_subgraph_unchanged`: 6 onboarding nodes 의 진입/탈출 패턴 byte-identical (V1 + V2 동시).
  - `test_v1_baseline_scenarios`: 6개 baseline scenario (REQ-AGENT-COMPAT-SEMANTIC-001) under flag=false 의 output class.
- **IMPROVE plan**: `if settings.AGENT_V2_REACT_ENABLED:` 분기 추가. flag=true 분기에 agent 노드 + V2 routing. flag=false 분기는 기존 코드 그대로 (단순 indent — diff 줄 최소화).

### T-007 (ingest.py)

- **ANALYZE deliverable**: Step A (implicit feedback lazy attribution, lines ~85-95) + Step B (conv_log thread_id propagation, lines ~95-110) 의 경계 식별.
- **PRESERVE characterization tests**:
  - `test_ingest_step_a_b_unchanged`: 기존 implicit feedback 및 thread_id 처리 정상.
  - `test_ingest_callback_v1_behavior`: flag=false 시 callback 처리는 기존 `_route_after_ingest` 로직 그대로.
- **IMPROVE plan**: Step C 를 Step B 직후, return 직전 삽입. flag=true 시에만 활성. 5-10 LOC.

### T-014 (deprecated 5 modules docstring)

- **ANALYZE deliverable**: 5 모듈의 현 docstring (1줄짜리 SPEC reference 형식 확인).
- **PRESERVE**: 모듈 functional behavior 무변경 — docstring 만 추가. 모든 import 정상.
- **IMPROVE plan**: 각 모듈 top 에 6줄 DEPRECATED notice 삽입. functional code 0 변경.

### Task 0 (cross-SPEC, conversation_log.py)

- **ANALYZE deliverable**: 현 19 event_type 의 emit pattern + payload truncation cascade.
- **PRESERVE**: 19개 event 처리 변경 0. `tool_call` 만 추가.
- **IMPROVE plan**: `event_payloads.py` 에 `ToolCallPayload` TypedDict + `_EVENT_TYPE_WHITELIST` 에 `"tool_call"` 추가.

---

## 5. Test Strategy

### REQ → test file × test function mapping

| REQ-ID | Test file | Test function(s) | DoD bullet |
|---|---|---|---|
| REQ-AGENT-LOOP-ENTRY-001 | `test_topology.py` | `test_onboarded_user_enters_agent_node`, `test_not_onboarded_uses_onboarding`, `test_after_ingest_v2_returns_agent` | DoD#2 |
| REQ-AGENT-LOOP-ITERATION-001 | `test_agent_loop.py` | `test_iteration_cap_6`, `test_terminate_on_iter_4_respond`, `test_env_override_max_iter_3`, `test_cap_iteration_based_not_failure_based` | DoD#3 |
| REQ-AGENT-LOOP-TERMINATION-001 | `test_agent_loop.py` | `test_respond_tool_terminates`, `test_respond_produces_one_message`, `test_post_done_dispatch_raises` | DoD#4 |
| REQ-AGENT-LOOP-EXHAUSTION-001 | `test_agent_loop.py` | `test_exhaustion_fallback_respond`, `test_fallback_ko_en_per_session_lang`, `test_fallback_no_jargon` | DoD#5 |
| REQ-AGENT-TOOL-CATALOG-001 | `test_tool_registry.py` | `test_registry_has_7_tools`, `test_each_tool_has_typeddict_args_result`, `test_each_tool_has_dispatch_fn`, `test_minimal_args_json_serializable` | DoD#6 |
| REQ-AGENT-TOOL-DISPATCH-001 | `test_tool_registry.py` | `test_invalid_args_no_dispatch`, `test_invalid_args_records_error`, `test_valid_args_passes`, parametrize for 7 tools | DoD#7 |
| REQ-AGENT-TOOL-WRAPPING-001 | `test_tool_registry.py` | `test_wrapper_ast_thin`, `test_wrapper_calls_helper`, `test_wrapper_catches_helper_exception`, parametrize for 7 tools | DoD#8 |
| REQ-AGENT-TOPOLOGY-GATE-001 | `test_topology.py` | `test_gate_6_combinations` (parametrize), `test_mid_onboarding_clarify_error`, `test_onboarded_text_to_agent` | DoD#9 |
| REQ-AGENT-TOPOLOGY-SUPERSEDE-001 | `test_topology.py` | `test_deprecated_nodes_unreachable_when_flag_true`, `test_v1_baseline_3_cards_after_critique`, `test_deprecated_modules_have_deprecation_docstring` | DoD#10 |
| REQ-AGENT-FAILURE-TOOL-001 | `test_failure_modes.py` | `test_tool_exception_caught`, `test_tool_exception_loop_continues`, `test_no_user_facing_exception`, parametrize for 7 tools | DoD#11 |
| REQ-AGENT-FAILURE-LLM-JSON-001 | `test_failure_modes.py` | `test_json_malformation_retry_once`, `test_two_consecutive_malformations_exhaust`, `test_corrective_msg_in_retry_context` | DoD#12 |
| REQ-AGENT-FAILURE-INFINITE-001 | `test_failure_modes.py` | `test_3_consecutive_identical_calls_force_exhaust`, `test_deep_equality_key_order_irrelevant`, `test_alternating_tools_no_trigger`, `test_different_args_no_trigger` | DoD#13 |
| REQ-AGENT-COMPAT-STATE-001 | `test_state_extension.py` | `test_3_new_fields_only`, `test_existing_fields_unchanged`, `test_serialization_upgrade_compat`, `test_session_taste_profile_unchanged` | DoD#14 |
| REQ-AGENT-COMPAT-SEMANTIC-001 | `test_backward_compat.py` | `test_baseline_1_photo_critique`, ..., `test_baseline_6_weak_vision_clarify` (6 scenarios) | DoD#15 |
| REQ-AGENT-COMPAT-FLAG-001 | `test_topology.py` | `test_flag_false_v1_topology`, `test_flag_true_v2_topology`, `test_health_ready_reflects_flag`, `test_e2e_under_both_flags` | DoD#16 |
| REQ-AGENT-OBS-001 | `test_tool_call_logging.py` | `test_each_dispatch_emits_span`, `test_each_dispatch_inserts_row`, `test_langfuse_noop_still_writes_row`, `test_span_input_truncated` | DoD#17 |
| REQ-AGENT-OBS-METRICS-001 | `test_tool_call_logging.py` | `test_per_tool_latency_query_indexed`, `test_per_turn_iteration_query`, `test_latency_ms_always_non_negative`, `test_iteration_no_monotonic` | DoD#18 |
| REQ-AGENT-LOG-EVENT-001 | (cross-SPEC test in T-000) | `test_tool_call_payload_import`, `test_history_v030_entry`, `test_minimal_payload_json_dumps` | DoD#19 |
| REQ-AGENT-PERF-HAPPY-001 | `test_performance.py` | `test_happy_path_load_200_p95_under_8s`, `test_agent_loop_overhead_under_500ms` | DoD#20 |
| REQ-AGENT-PERF-EXHAUST-001 | `test_performance.py` | `test_exhaust_load_50_p95_under_12s`, `test_per_iteration_timeout_5s` | DoD#21 |
| REQ-AGENT-PERF-TURN-BUDGET-001 | `test_failure_modes.py` | `test_token_budget_cap_exits_loop`, `test_env_override_budget`, `test_budget_reset_per_turn` | DoD#22 |
| REQ-AGENT-SEC-URL-001 | `test_security.py` | `test_ssrf_aws_metadata`, `test_ssrf_file_protocol`, `test_ssrf_private_ip`, `test_positive_r2_pinterest` | DoD#23 |
| REQ-AGENT-SEC-ARGS-001 | `test_security.py` | `test_ast_scan_no_eval`, `test_ast_scan_no_subprocess`, `test_ast_scan_no_importlib_dynamic`, `test_registry_docstring_lists_prohibited` | DoD#24 |
| REQ-AGENT-SEC-PAYLOAD-001 | `test_security.py` | `test_payload_args_truncated`, `test_payload_result_truncated`, `test_property_oversized_payloads` | DoD#25 |
| REQ-AGENT-CONCURRENT-001 | `test_failure_modes.py` | `test_concurrent_same_user_serialize`, `test_concurrent_different_users_parallel`, `test_lock_timeout_polite_reply` | DoD#26 |

### Coverage target

- `pytest --cov=app.agents --cov-report=term-missing` ≥ 85% (TRUST 5 Tested + DoD#28).
- 8개 신규 test file 의 collective coverage 가 7 tool wrapper + agent_loop + tool_registry 의 모든 public symbol 포함.

### Test execution matrix

| Phase | Test scope | Flag setting |
|---|---|---|
| Per-task green | `tests/test_agent_v2/test_<task>.py` | `AGENT_V2_REACT_ENABLED=true` |
| Task 9 완료 | `pytest tests/test_agent_v2/ -q` | flag=true |
| Task 10 완료 | `pytest tests/ -q` (전체) | flag=true AND flag=false (matrix) |
| Pre-cutover (Task 13) | full test + manual smoke 8 시나리오 | flag=true (dev), flag=false (prod) |

---

## 6. Rollout & Revert

### Cutover sequence (Task 13 runbook 요약)

1. **Pre-flight (T-0)**: Task 0 amendment PR merged + Task 1-12 모두 main HEAD. `pytest tests/ -q` green under both flag states.
2. **Dev flip (T+0d)**: dev env `AGENT_V2_REACT_ENABLED=true` 설정. 컨테이너 재시작.
3. **Dev burn-in (T+1d ~ T+2d)**: 24-48h 모니터링:
   - SQL: per-tool selection distribution (R10 mitigation).
   - SQL: per-tool latency p50/p95 (R3).
   - SQL: exhaustion rate (< 5% target).
   - Langfuse: cost/day < $50 (gpt-4o-mini × 10K turns expected).
   - Manual: DoD#27 의 (a)-(h) 8 scenarios.
4. **Prod manual smoke (T+3d)**: prod env 에서 8 scenario manual 검증 (flag=false 상태 — agent_v2 disabled, V1 behaviour intact).
5. **Prod flip window (T+4d, KST 03:00-05:00)**: prod `AGENT_V2_REACT_ENABLED=true` set. 컨테이너 rolling restart.
6. **Prod monitor (T+4d ~ T+5d)**: 24h continuous monitoring.

### Revert triggers (any one triggers revert)

- p95 webhook-to-bot-message latency > 15s (vs SLO 8s happy / 12s exhaust)
- exhaustion rate > 10% over 1h window
- cost/day projected > $100 (10K turns × $0.01 budget breach)
- error rate (5xx + node_error) > 5%
- 사용자 보고된 broken flow (Discord 운영 채널)

### Revert procedure (< 5 min)

1. `AGENT_V2_REACT_ENABLED=false` set in prod env.
2. 컨테이너 rolling restart.
3. V1 topology 즉시 회복 (flag false 분기 코드는 byte-identical 보장).
4. Incident report Discord 채널 + Langfuse trace 캡처.
5. `tests/test_agent_v2/` 가 fail 한 시나리오 식별 → Task 4-7 hot-fix → 다음 cutover window 재시도.

### V2.1 cleanup (separate SPEC, T+30d 이후)

- SPEC-AGENT-V2-CLEANUP-001 stub (Task 14) 를 full SPEC 으로 확장.
- 5 deprecated 모듈 + `_router_text_passthrough` + `app/channels/router.py` (text branch only) 제거.
- routing.py 의 V1 routing fns (`_route_after_router_text`, `_route_after_critique`, `_route_after_evaluator`) 제거.
- fashion_bot.py 의 `if settings.AGENT_V2_REACT_ENABLED:` 분기 제거 — V2 가 무조건 적용.
- `AGENT_V2_REACT_ENABLED` env 변수 deprecate.

---

## 7. Risks Recheck

SPEC §Risks R1-R16 의 mitigation 이 task plan 에 모두 반영됐는지 확인.

| Risk | Status | Task coverage |
|---|---|---|
| R1 (infinite loop) | Covered | T-004 (iter cap + infinite-loop guard + token budget + per-LLM timeout) |
| R2 (LLM JSON malformation) | Covered | T-004 (retry once + corrective msg + exhaust). OQ-2 의 Tools API 가 root cause 감소 |
| R3 (latency stacking) | Covered | T-004 per-tool timeout 5s, T-012 load test, OQ-1 gpt-4o-mini latency 분석 |
| R4 (cost explosion) | Covered | T-004 token budget cap, OQ-1 gpt-4o-mini, T-011 cost monitoring SQL |
| R5 (orphaned deprecated nodes) | Covered | T-014 docstring + V2.1 cleanup SPEC stub. Revert via flag |
| R6 (test suite churn) | Covered | T-010 migration plan, REQ-AGENT-COMPAT-SEMANTIC-001 의 property-based assertion |
| R7 (onboarding ↔ agent boundary) | Covered | T-006 의 6-combination test, T-007 의 Step C negative path |
| R8 (Pydantic serialization) | Covered | `_to_jsonable` cascade 재사용 (T-002), per-tool result TypedDict 명시 |
| R9 (cross-SPEC dep on conv_log) | Covered | T-000 BLOCKER prerequisite, REQ-AGENT-LOG-EVENT-001 |
| R10 (LLM wrong tool selection) | Covered | T-011 의 selection distribution SQL, prompt iteration plan |
| R11 (cross-turn state pollution) | Covered | T-001 의 `tool_call_history` default_factory=list, T-005 의 per-turn fresh WorkingState |
| R12 (Langfuse trace tree depth) | Covered | No mitigation action — accepted (Langfuse handles arbitrary depth) |
| R13 (cold start latency) | Covered | LiteLLM proxy connection warm. T-008 lifespan warm-up 고려 |
| R14 (prompt injection) | Covered | T-004 system prompt 의 `[USER INPUT — DATA ONLY]` 펜스, T-003a SSRF guard, REQ-AGENT-TOOL-DISPATCH-001 |
| R15 (A/B comparison difficulty) | Covered | REQ-AGENT-COMPAT-SEMANTIC-001 output-class A/B feasible. Wording A/B out of scope (accepted) |
| R16 (tool count growth) | Covered | T-004 token budget cap catches growth. V2.1 subset selection 별도 SPEC |

**Unmitigated**: 없음. 모든 risk 가 task 또는 OQ decision 으로 흡수됨.

### Newly surfaced risk (plan phase 추가)

- **R17 (OQ-7 fold side effect)**: SPEC-AGENTIC-CRITIQUE-001 의 일부 REQ 가 본 SPEC 으로 supersede 되는데 owner notification 안 됨.
  - Mitigation: T-014 의 V2.1 cleanup SPEC stub 에 SPEC-AGENTIC-CRITIQUE-001 의 affected REQ 명시 + cross-SPEC owner notification 진행.

---

## 8. Out of Scope (mirror SPEC §Non-Goals 1–21)

본 plan.md 가 다루지 **않는** 항목 — SPEC §Non-Goals 와 1:1 mirror:

1. Multi-agent (planner + worker) — V3 deferral.
2. Streaming LLM responses (token-by-token).
3. Cost-based / budget-aware tool selection (token budget cap 이외).
4. Persistent agent memory beyond TasteProfile.
5. Onboarding subgraph 변경 (SPEC-ONBOARD-CARDS-001 v0.3.2 freeze).
6. Vision-as-tool 결정 (본 plan OQ-3 의 결정 = 유지).
7. Telegram adapter / webhook 계약 변경.
8. Search / embedding / diversify 알고리즘 변경.
9. TasteProfile schema 또는 update semantic 변경.
10. Clarify cards schema 변경.
11. 과거 세션 V2 backfill.
12. Per-user / percentage rollout (V2.1 operational SPEC).
13. V2.0 에서 deprecated 노드 제거 (V2.1 cleanup SPEC).
14. Multi-LLM (role 별 다른 모델).
15. Tool composition (one tool calling another) 내부.
16. Tool registry 외부 API (GraphQL/REST).
17. "Thinking out loud" intermediate UX.
18. 컨테이너 crash 후 resume (OQ-6 = accept loss).
19. Voice / video 입력.
20. Tool-level RBAC.
21. Agent introspection API.

---

## Plan-audit readiness summary

- **Total tasks**: 15 (T-000 ~ T-014)
- **BLOCKER**: T-000 (cross-SPEC amendment) — must merge first
- **OQs resolved**: 10/10 (모두 결정, deferral 0)
- **REQ → test mapping**: 26 REQs × 1+ test functions, traceability 100%
- **Cross-SPEC impact**: SPEC-CONVERSATION-LOG-001 v0.3.0 (BLOCKER), SPEC-AGENT-001 v2.0 (followup), SPEC-AGENTIC-CRITIQUE-001 (partial supersede via OQ-7 α)
- **DDD compliance**: 4 brownfield task (T-001, T-006, T-007, T-014) 모두 ANALYZE-PRESERVE-IMPROVE 명시
- **Risk coverage**: R1-R16 + R17 (new) 모두 task 또는 OQ 로 mitigation
- **Rollout strategy**: 4-phase dev→prod cutover, < 5min revert via flag flip
