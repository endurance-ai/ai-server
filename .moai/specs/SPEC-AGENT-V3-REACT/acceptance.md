# SPEC-AGENT-V3-REACT — Acceptance Criteria

> Given/When/Then. 갭별 최소 2개 + byte-identical 회귀 + 성능 가드 + edge case.
> baseline = SPEC-AGENT-V2-REACT (마스터 `AGENT_V2_REACT_ENABLED=true`, `AGENT_LLM_MODEL` set, 4 sub-flag off).

---

## Gap 1 — 메모리 자동 주입 (REQ-AGENT-V3-MEM-*)

### AC-1.1 — 취향·맥락 자동 주입 (flag ON)

- **Given** `AGENT_V3_MEMORY_INJECTION_ENABLED=true`, user_key `u:7` 의 TasteProfile `liked_brands={"ami":2.0}`, conv_log 에 최근 3개 event (user_text "트렌치 보여줘" 등),
- **When** `run_react_loop(state, sess)` 가 실행되어 첫 LLM call 이 발생,
- **Then** mock LLM 입력 캡처에서 첫 LLM call 의 system 메시지가 `[MEMORY CONTEXT — SYSTEM DERIVED]` 펜스를 포함하고, 그 펜스 안에 문자열 `"ami"` 와 시드한 3개 conv event 의 요약 텍스트(예: `"트렌치 보여줘"` 의 200자-cap 요약)가 **모두 존재**하며, 동일 turn 의 `tool_call_history` 에 `get_recent_history` 호출 entry 가 **0건**임을 단언한다 (LLM 이 명시 호출 없이 컨텍스트를 수신).

### AC-1.2 — fail-soft (빈 메모리, in_memory backend)

- **Given** flag ON, TasteProfile 빈, conv_log backend in_memory (`get_recent_history` → 빈 list),
- **When** `run_react_loop` 실행,
- **Then** 메모리 블록은 빈 placeholder ("(no taste history yet)") 이고, 루프는 예외 없이 정상 진행하며 정상 응답을 발화한다.

### AC-1.3 — token cap 준수 + 최신 우선 (edge)

- **Given** flag ON, `AGENT_V3_MEMORY_MAX_TOKENS=200`, conv_log 에 50개 event + TasteProfile 에 50개 liked_keywords,
- **When** `run_react_loop` 실행,
- **Then** 메모리 블록 char 길이 ≤ 200*4, 가장 최근 턴이 보존되고 오래된 턴이 잘리며, 주입 후에도 `cumulative_tokens >= AGENT_TURN_TOKEN_BUDGET` exhaustion guard 가 정상 동작한다.

### AC-1.4 — flag OFF byte-identical (회귀)

- **Given** `AGENT_V3_MEMORY_INJECTION_ENABLED=false`,
- **When** `run_react_loop` 실행,
- **Then** 첫 LLM call 의 messages 가 V2 baseline 스냅샷 `[{system:_SYSTEM_PROMPT},{user:_build_user_message}]` 와 byte-exact 일치, `_memory_context`/`get_recent_history` 메모리 경로 0회 호출 (spy).

---

## Gap 2 — Reflexion 루프 (REQ-AGENT-V3-REFLEX-*) — V2 OQ-7 resolution

### AC-2.1 — search 결과 평가 후 LLM 자율 refine (flag ON)

- **Given** flag ON, mock LLM iter1=`search_products(text_query="cheap bag")`, search 가 5 candidates 반환,
- **When** dispatch 완료 후 Reflexion 평가가 `CritiqueScore(score=0.3, retry=True)` 산출,
- **Then** 다음 LLM call 의 ToolMessage content 에 `"_quality":{"score":0.3,"retry_suggested":true,...}` 포함, mock LLM 이 자율적으로 `refine_search` 호출 → 정상 dispatch, 강제 refine 아님.

### AC-2.2 — 기존 evaluator 헬퍼 래핑 (재구현 금지 증거)

- **Given** flag ON, search 가 5 candidates 반환,
- **When** Reflexion 평가 발생,
- **Then** `evaluator._call_llm` 이 정확히 1회 호출됨 (mock spy), `_reflexion.py` 의 AST 검사에서 자체 LLM 평가 프롬프트·평가 함수 신규 정의 부재, evaluator timeout 시 fail-open `CritiqueScore(score=1.0,retry=False)` 가 그대로 전파됨.

### AC-2.3 — bounded: 호출 횟수 cap + infinite-loop 무충돌 (REQ-AGENT-V3-REFLEX-BOUND-001)

- **Given** flag ON, mock LLM 이 search_products 를 6회 호출 시도,
- **When** 루프 진행,
- **Then** evaluator 호출은 최대 `SELF_CRITIQUE_MAX_ITERATIONS`(2) 회, react_loop iteration counter 가 evaluator 호출로 인해 미증가, 동일 args 3연속 시 기존 infinite-loop guard 정상 트리거이며 평가 전후 `tool_call_history` 가 동일(Reflexion 이 history 미오염).

### AC-2.4 — flag OFF byte-identical (회귀)

- **Given** `AGENT_V3_REFLEXION_ENABLED=false`,
- **When** search_products dispatch,
- **Then** ToolMessage content 가 V2 `json.dumps(result,default=str)[:2000]` 와 byte-exact, `"_quality"` 키 부재, `evaluator._call_llm` (Reflexion 경로) 0회 호출.

### AC-2.5 — 잔여-budget timeout 강제 취소 (REQ-AGENT-V3-REFLEX-DEADLINE-001, D2)

- **Given** flag ON, `EVALUATOR_TIMEOUT_S=8` (기본), slow evaluator stub (응답까지 20s 소요), turn_deadline 까지 잔여 ≈2s 인 시점에서 search_products dispatch 가 성공 완료,
- **When** Reflexion 평가가 트리거되어 `await asyncio.wait_for(evaluate_search_quality(...), timeout=remaining≈2s)` 로 wrap 실행,
- **Then** evaluator 호출이 잔여-budget 경계(≈2s, **8s 가 아님**)에서 `asyncio.TimeoutError` 로 **취소**되고, 해당 ToolMessage 는 `_quality` skipped 마커(또는 미첨부)로 진행되며, turn 완료 시각 ≤ `turn_deadline` 이고 turn 이 상속 예산(p95<8s) 안에 종료됨을 단언한다 (mechanical: stub 주입 + 취소 발생 시각 단언 + turn 종료 시각 ≤ turn_deadline 단언).

### AC-2.6 — 잔여 budget 0 / 충분 경계 (REQ-AGENT-V3-REFLEX-DEADLINE-001 positive·zero control)

- **Given** flag ON,
- **When** (a) `remaining ≤ 0` 인 상태에서 dispatch 성공 / (b) 빠른 evaluator(잔여 budget 충분)로 dispatch 성공,
- **Then** (a) evaluator 0회 호출(skip), 즉시 다음 단계 진행 / (b) 정상 평가 완료, ToolMessage 에 `_quality` 첨부 (positive control — 정상 경로가 깨지지 않음 증명).

---

## Gap 3 — 능동 제안 (REQ-AGENT-V3-PROACT-*)

### AC-3.1 — 8번째 tool 등록 + 발송 (flag ON)

- **Given** `AGENT_V3_PROACTIVE_ENABLED=true`,
- **When** REGISTRY 로드 + mock LLM 이 `suggest_next_step(options=["더 캐주얼","다른 무드"])` 호출,
- **Then** `TOOL_NAMES` 길이 8 + `"suggest_next_step"` 포함, `validate_args` 동작, dispatch 가 adapter `send_text_with_buttons` 호출 (mock), `terminates_loop=False` 라 루프 미종결.

### AC-3.2 — system prompt 능동성 지침 (flag ON)

- **Given** flag ON, search 가 1 candidate (약함),
- **When** 첫 LLM call,
- **Then** system 메시지에 능동성 지침 (candidates 약 → suggest_next_step 선제 / 모호 → ask_user_clarification 선제) 문자열 포함, mock LLM 이 `suggest_next_step` 호출 가능.

### AC-3.3 — flag OFF REGISTRY 7-tool + prompt byte-identical (회귀)

- **Given** `AGENT_V3_PROACTIVE_ENABLED=false`,
- **When** REGISTRY 로드 + `run_react_loop`,
- **Then** `TOOL_NAMES == ("analyze_image","search_products","refine_search","update_taste","ask_user_clarification","get_recent_history","respond")` (V2 7-tool 정확), system 메시지 = V2 `_SYSTEM_PROMPT` byte-exact.

---

## Gap 4 — 크로스스레드 dislike (REQ-AGENT-V3-DISLIKE-*)

### AC-4.1 — dislike + timestamp 기록, 직렬화 호환 (flag ON)

- **Given** flag ON,
- **When** `update_taste(source="free_text", brand_dislikes=["zara"])` dispatch,
- **Then** TasteProfile 의 `disliked_brands_ts["zara"]` 에 ts 기록 + `disliked_brands["zara"]` score 정상 누적, `taste_profile_pg` `_jsonable` 직렬화 `json.dumps` 성공, 새 dict 없는 구 row 역직렬화 시 빈 dict default (KeyError 없음).

### AC-4.2 — 크로스스레드 자동 디스카운트 (flag ON)

- **Given** flag ON, thread A 에서 `update_taste(brand_dislikes=["gucci"])` 후,
- **When** 새 thread B 에서 `search_products(text_query="bag")` dispatch,
- **Then** dispatch 의 exclude 입력에 "gucci" 가 머지됨 (검색 입력 캡처), recency 가중으로 최근 dislike 가 오래된 것보다 강하게 discount (헬퍼 단위 테스트), 기존 exclude 경로 재사용 (AST: 새 ranking 함수 부재).

### AC-4.3 — flag OFF TasteProfile byte-identical (회귀)

- **Given** `AGENT_V3_DISLIKE_MEMORY_ENABLED=false`,
- **When** `update_taste(brand_dislikes=["zara"])` 후 `search_products` dispatch,
- **Then** `disliked_brands_ts` 빈 상태 (기록 안 함), `disliked_brands` 만 V2 처럼 갱신, search dispatch 의 exclude 입력이 V2 baseline 과 byte-identical, TasteProfile 필드 타입/기본값 V2 무변경.

---

## 횡단 — byte-identical 회귀 (REQ-AGENT-V3-COMPAT-ALLOFF-001)

### AC-X.1 — 4 sub-flag all-off → 전체 byte-identical V2 (핵심 단일 가드)

- **Given** 4 sub-flag 모두 false (마스터 ON),
- **When** 대표 6 시나리오 실행: (1) 사진+"비슷한 거" (2) "운동복" 텍스트검색 (3) "더 저렴한 거" (4) "ami 좋아해" 취향 (5) "안녕" off-topic (6) 모호 "옷" clarify,
- **Then** 각 시나리오의 LLM 입력 messages, REGISTRY (`TOOL_NAMES`), 모든 ToolMessage content, TasteProfile 필드집합/직렬화가 V2 baseline 스냅샷과 **byte-exact** 일치하며, 기존 `tests/test_agent_v2/` 전 스위트가 무변경 통과한다.

### AC-X.2 — wrap-only 정적 검증 (REQ-AGENT-V3-COMPAT-WRAP-001)

- **Given** 전체 변경분,
- **When** AST/grep 정적 분석,
- **Then** `_memory_context.py`/`_reflexion.py`/Gap4 변경분이 명시된 기존 헬퍼를 import+호출하며, 새 LLM 호출 프롬프트·새 메모리 요약·새 검색/ranking 함수를 정의하지 않고, `_reflexion.py` 가 `LLMProvider.chat` 직접 호출 없이 `evaluator._call_llm` 경유한다.

---

## 성능 가드 (REQ-AGENT-V3-PERF-001)

### AC-P.1 — 4 flag ON happy-path p95 < 8s

- **Given** 4 sub-flag 모두 ON,
- **When** 200턴 mixed happy-path (50 사진+검색 / 50 텍스트검색 / 50 critique / 50 off-topic) 부하 (V2 하니스 재사용),
- **Then** end-to-end webhook→respond p95 < 8s (SPEC-AGENT-V2-REACT REQ-AGENT-PERF-HAPPY-001 미초과).

### AC-P.2 — 4 flag ON exhausted p95 < 12s + slow-evaluator 잔여-budget 취소 + Gap1 조립 오버헤드

- **Given** 4 flag ON, 50턴 중 일부에 slow evaluator stub (응답 20s — REQ-AGENT-V3-REFLEX-DEADLINE-001 / AC-2.5 와 정합) 주입,
- **When** 50턴 forced-exhaustion 부하 + Gap1 메모리 조립 단위 perf 측정,
- **Then** end-to-end p95 < 12s (REQ-AGENT-PERF-EXHAUST-001 미초과) **이고** 동시에 happy-path 부분집합 p95 < 8s (REQ-AGENT-PERF-HAPPY-001 미초과), slow-evaluator turn 들이 잔여-budget timeout 으로 강제 취소되어 turn_deadline 안에 종료(p95 가 evaluator 8s 로 인해 부풀지 않음), `build_memory_context` 조립 오버헤드 < 50ms.

---

## 보안 (REQ-AGENT-V3-SEC-001)

### AC-S.1 — 메모리 주입 이중 격리 + truncation

- **Given** flag ON, 과거 turn 에 5000자 user_text + 악의적 "ignore previous instructions" 입력 포함,
- **When** `run_react_loop` 실행,
- **Then** 주입된 해당 요약이 200자로 cap (`_summarize_payload`), 메모리 블록이 `[MEMORY CONTEXT — SYSTEM DERIVED]` 펜스로 구획, `_build_user_message` 의 `[USER INPUT — DATA ONLY]` 펜스가 V2 그대로 유지 (이중 격리), 메모리 블록 전체 char ≤ `AGENT_V3_MEMORY_MAX_TOKENS`*4.

---

## Edge cases (추가)

| # | Given | When | Then |
|---|---|---|---|
| E1 | flag1 ON 나머지 OFF | run_react_loop | Gap1 만 활성, Gap2/3/4 경로 byte-identical V2 (직교성 검증) |
| E2 | Gap2 ON + Gap4 ON (pairwise) | search dispatch | Reflexion 평가 + dislike exclude 동시 적용, 상호 간섭 없음 (R9) |
| E3 | Gap1 ON + Gap3 ON (pairwise) | 첫 LLM call | system = `_SYSTEM_PROMPT + _PROACTIVE_DIRECTIVE + memory_context` 순서 정확, 둘 다 cap 내 |
| E4 | flag ON, evaluator `_call_llm` raises | Reflexion 평가 | fail-open score=1.0 전파, 루프 정상 진행, 사용자 응답 보장 |
| E5 | flag ON, 마스터 `AGENT_V2_REACT_ENABLED=false` | webhook | V1 토폴로지 — V3 sub-flag 전부 no-op (마스터 게이트 우선) |
| E6 | Gap4 ON, 동일 브랜드를 dislike 후 like (update_taste) | 후속 search | 기존 `reinforce_*` 의 like→dislike pop semantics 그대로, ts 는 마지막 dislike 시점만 (모순 신호 방지) |

---

## Definition of Done (P0)

- [ ] Task 1-7 완료. 5 env 추가, `AGENT_V2_*` 패턴 일치.
- [ ] AC-1.* ~ AC-4.* (갭별 ≥2, flag ON + OFF byte-identical) 통과.
- [ ] AC-2.5 + AC-2.6 (REQ-AGENT-V3-REFLEX-DEADLINE-001 — 잔여-budget timeout 강제 취소 mechanical + zero/positive control) 통과 — **D2 P0 perf bound 기계 보장**.
- [ ] AC-X.1 (4 all-off byte-identical, 6 시나리오) + AC-X.2 (wrap-only AST) 통과 — **단일 핵심 회귀 가드**.
- [ ] AC-P.1/P.2 성능 가드 통과 (V2 예산 미초과).
- [ ] AC-S.1 이중 격리 + truncation 통과.
- [ ] E1-E6 edge 통과.
- [ ] 기존 `tests/test_agent_v2/` + 전 기존 스위트가 4 all-off 에서 무변경 통과.
- [ ] `tests/test_agent_v3/` 신규 — `app/agents/_memory_context.py`/`_reflexion.py`/`tools/suggest_next_step.py` + Gap4 변경분 ≥ 85% line coverage.
- [ ] `evaluator.py`/`taste_profile.py` (store Protocol)/`conversation_log.py` 의 기존 본문 무변경 (헬퍼 import 만; AST 검증).
- [ ] `WorkingState`/`fashion_bot.py` 토폴로지 무변경 (snapshot).
- [ ] `ruff check . && ruff format --check .` 통과. `pytest -q` 가 pre-SPEC baseline 동수 이상.
- [ ] dev 봇 수동 스모크: 각 flag 단독 ON 으로 갭 동작 확인 + 4 all-off 로 V2 동작 확인.
