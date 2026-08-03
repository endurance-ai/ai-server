# SPEC-AGENT-V3-REACT — Implementation Plan

> Brownfield Delta plan. V2 ReAct 루프 위 4개 갭을 sub-flag 별 독립 구현·롤백.
> 시간 추정 금지 — priority + phase ordering 사용.

---

## 0. 원칙

- [HARD] **Wrap, never reimplement.** Gap1=`get_recent_history` dispatch + `TasteProfile.boost_*`; Gap2=`evaluator._call_llm`/`_build_fastpath_delta`/`_evaluator_prompt.build_user_prompt`; Gap4=`TasteProfile.reinforce_disliked_*`/`exclude_brands`/`_jsonable`.
- [HARD] **각 sub-flag OFF → 해당 경로 byte-identical V2.** 4 all-off → 전체 byte-identical V2 (단일 회귀 가드).
- [HARD] **루프 메커니즘 무변경.** iteration cap / token budget / per-tool·LLM timeout / infinite-loop guard / transient retry / turn_deadline — 한 줄도 수정 안 함.
- [HARD] **그래프 토폴로지 무변경.** `fashion_bot.py` 무수정. WorkingState 무수정.
- TDD (brownfield enhancement): 기존 코드 read → failing test → minimal → refactor.

---

## 1. Task 분해 (priority 순)

### Task 1 — 환경 변수 + flag 인프라 [P0, 선행]

- `app/core/config.py::Settings` 에 5 env 추가 (`AGENT_V2_*` 블록 바로 아래, 동일 패턴):
  `AGENT_V3_MEMORY_INJECTION_ENABLED`(bool=False), `AGENT_V3_REFLEXION_ENABLED`(bool=False), `AGENT_V3_PROACTIVE_ENABLED`(bool=False), `AGENT_V3_DISLIKE_MEMORY_ENABLED`(bool=False), `AGENT_V3_MEMORY_MAX_TOKENS`(int=1500).
- `.env.example` 업데이트 + `docs/infra/env.md` (Sync phase).
- 회귀: 5 env 미설정 시 전부 false → 기존 모든 테스트 무변경 통과.
- MX: env 블록에 `# @MX:SPEC: SPEC-AGENT-V3-REACT`.

### Task 2 — Gap1 메모리 자동 주입 [P1]

- NEW `app/agents/_memory_context.py`:
  - `build_memory_context(state, sess, ctx, *, max_tokens) -> str` — (a) `get_taste_store().get_or_create(user_key)` → `boost_brands(5)`/`boost_keywords(5)`/`exclude_brands()` 요약; (b) `get_recent_history.dispatch({"n": N}, ctx)` 호출해 events 요약 (N=**5**, OQ-V3-4 resolved); char-cap (`max_tokens*4`), 최신 우선 보존; `[MEMORY CONTEXT — SYSTEM DERIVED]` … `[/MEMORY CONTEXT]` 펜스.
- MODIFY `app/agents/react_loop.py` `run_react_loop` 의 초기 `messages` 구성 지점:
  - `if settings.AGENT_V3_MEMORY_INJECTION_ENABLED:` system content = `_SYSTEM_PROMPT + "\n\n" + build_memory_context(...)` else `_SYSTEM_PROMPT` (V2 byte-identical).
  - `_build_user_message` 의 `[USER INPUT — DATA ONLY]` 펜스 **무수정**.
- 롤백: flag OFF 1회로 즉시 V2. `_memory_context.py` 는 dead code (호출 안 됨).
- 테스트: REQ-AGENT-V3-MEM-INJECT/CAP/FLAG-001 + SEC-001.
- MX: `_memory_context.py` 모듈 docstring `@MX:NOTE [AUTO] wraps get_recent_history+TasteProfile, no new logic`.

### Task 3 — Gap2 Reflexion 루프 (V2 OQ-7 resolution) [P1]

- NEW `app/agents/_reflexion.py`:
  - `async evaluate_search_quality(state, sess, ctx) -> dict` — `sess.last_results` 풀세트(OQ-V3-1 권장) + `sess.user_intent` + vision_item 으로 `_evaluator_prompt.build_user_prompt` → `evaluator._call_llm` (빈 결과면 `_build_fastpath_delta`). 반환 `{"score": float, "retry_suggested": bool, "reason": str}` (CritiqueScore 에서 파생). evaluator fail-open(score=1.0) 그대로 전파.
- MODIFY `react_loop.py` search/refine dispatch 직후 (`run_react_loop` 의 `messages.append(ToolMessage(...))` 직전):
  - `if settings.AGENT_V3_REFLEXION_ENABLED and tool_name in ("search_products","refine_search") and result.get("ok"):`
    - **호출 횟수 cap** (REQ-AGENT-V3-REFLEX-BOUND-001): per-turn ctx 카운터 `_v3_reflexion_count` < `SELF_CRITIQUE_MAX_ITERATIONS` 확인. evaluator 호출은 ReAct iteration counter 미증가 + `tool_call_history` append 무영향 (infinite-loop guard 보존).
    - **잔여-budget timeout wrap** (REQ-AGENT-V3-REFLEX-DEADLINE-001): `run_react_loop` 의 기존 `turn_deadline` (per-turn 벽시계 ceiling) 을 재사용해 `remaining = max(0.0, turn_deadline - time.monotonic())` 계산. `remaining <= 0` 이면 evaluator skip. 그 외 `q = await asyncio.wait_for(evaluate_search_quality(...), timeout=remaining)` — overrun 시 `asyncio.TimeoutError` 로 **호출 강제 취소**, `q = {"skipped": True, "reason": "deadline"}`. pre-check 만으로는 불충분 (`EVALUATOR_TIMEOUT_S`=8s 가 `remaining` 보다 클 수 있음) — wait_for 취소가 normative.
    - `result = {**result, "_quality": {...}}` 후 `ToolMessage(content=json.dumps(result, default=str)[:2000], ...)`. skip 시 `_quality` 미첨부 또는 skipped 마커.
  - flag OFF → ToolMessage content V2 byte-identical (`json.dumps(result,...)[:2000]`, evaluator 0 호출).
- 롤백: flag OFF 즉시 V2. `_reflexion.py` dead code.
- 테스트: REQ-AGENT-V3-REFLEX-EVAL/DELTA/BOUND/DEADLINE/FLAG-001 (acceptance.md AC-2.1~2.5 + AC-P.2 정합 — slow-evaluator stub 으로 잔여-budget 경계 취소 + turn ≤ turn_deadline 단언).
- MX: `_reflexion.py` docstring `@MX:NOTE [AUTO] wraps evaluator._call_llm — OQ-7 resolution (in-loop, not graph node)`; react_loop 주입 지점 `@MX:WARN @MX:REASON Reflexion call MUST be wrapped in asyncio.wait_for(timeout=remaining turn budget) — pre-check insufficient (EVALUATOR_TIMEOUT_S > residual)`.

### Task 4 — Gap3 능동 제안 [P1]

- NEW `app/agents/tools/suggest_next_step.py`:
  - `async dispatch(args, ctx) -> SuggestNextStepResult` — `_adapter_ctx.get_adapter()` + `send_text_with_buttons` 재사용 (ask_user_clarification.py 패턴 미러). 옵션 카드 (유사 아이템/핏 변경/다른 무드). callback shape `suggest:{kind}:{value}`. line ≤ 80.
- MODIFY `app/agents/tool_registry.py`:
  - `SuggestNextStepArgs`/`SuggestNextStepResult` TypedDict + `__all__` 추가.
  - REGISTRY 빌드를 flag-aware: 모듈 로드 시점 (OQ-V3-6 권장) `if settings.AGENT_V3_PROACTIVE_ENABLED:` 8번째 entry 추가. `TOOL_NAMES` 가 자동 반영.
  - flag OFF → REGISTRY 정확히 V2 7-tool, byte-identical.
- MODIFY `react_loop.py` `_SYSTEM_PROMPT` 사용 지점:
  - `_PROACTIVE_DIRECTIVE` 모듈 상수 신설. flag ON 시 system content 에 append (Gap1 과 조립 순서: `_SYSTEM_PROMPT [+_PROACTIVE_DIRECTIVE] [+memory_context]`). flag OFF → 미append.
  - 지침: candidates_count < N (OQ-V3-5 권장 3) 약함 → `suggest_next_step` 선제; 모호도 낮음 → `ask_user_clarification` 선제.
- 롤백: flag OFF 즉시 V2 7-tool + V2 prompt.
- 테스트: REQ-AGENT-V3-PROACT-TOOL/PROMPT/FLAG-001.
- MX: `suggest_next_step.py` `@MX:NOTE [AUTO] Side effect: Telegram inline-keyboard send (reuses adapter)`.

### Task 5 — Gap4 크로스스레드 dislike 메모리 [P1]

- MODIFY `app/channels/taste_profile.py::TasteProfile`:
  - additive 필드 `disliked_brands_ts: dict[str, float] = field(default_factory=dict)`, `disliked_keywords_ts: dict[str, float] = field(default_factory=dict)` (OQ-V3-3 권장 단일 last-ts).
  - 신규 메서드 `recency_weighted_excludes(now, *, half_life_s) -> tuple[list[str], list[str]]` — 기존 `exclude_brands()` + ts 기반 exponential recency 가중 (OQ-V3-7 권장; 기존 decay 0.9 와 일관). 기존 `reinforce_disliked_*`/`exclude_brands`/`_cap` **무변경**. `TasteProfileStore` Protocol/`update()` 무변경.
- MODIFY `app/channels/taste_profile_pg.py`: 새 2 dict 를 기존 `_jsonable` cascade 직렬화/역직렬화에 포함. 구 row 는 빈 dict default. `update()` 시그니처 무변경.
- MODIFY `app/agents/tools/update_taste.py`:
  - `if settings.AGENT_V3_DISLIKE_MEMORY_ENABLED:` `reinforce_disliked_*` 호출 직후 `profile.disliked_*_ts[key]=time.time()` 기록. flag OFF → V2 byte-identical (ts 미기록).
- MODIFY `app/agents/tools/search_products.py` + `refine_search.py`:
  - `if settings.AGENT_V3_DISLIKE_MEMORY_ENABLED:` dispatch 진입 시 store 에서 `recency_weighted_excludes` 읽어 기존 `exclude_keywords` 경로 / `AnalyzedItem` 에 머지 (기존 exclude 경로 재사용 — 새 ranking 금지). flag OFF → exclude 입력 V2 byte-identical.
- 롤백: flag OFF 즉시 V2 (ts 미기록 + 디스카운트 미적용). 새 dict 는 존재하되 비어있음 (스키마 호환).
- 테스트: REQ-AGENT-V3-DISLIKE-SCHEMA/DISCOUNT/FLAG-001.
- MX: TasteProfile 신규 필드 `@MX:SPEC SPEC-AGENT-V3-REACT`; `recency_weighted_excludes` `@MX:NOTE [AUTO] additive — reuses exclude_brands, no schema-break`.

### Task 6 — 횡단 검증 [P0]

- `tests/test_agent_v3/`:
  - `test_byte_identical.py` — REQ-AGENT-V3-COMPAT-ALLOFF-001 (4 all-off, 6 시나리오, V2 baseline 스냅샷 byte-exact).
  - `test_wrap_only.py` — REQ-AGENT-V3-COMPAT-WRAP-001 (AST: 헬퍼 import+호출, 신규 LLM/평가/ranking 함수 부재).
  - `test_performance_v3.py` — REQ-AGENT-V3-PERF-001 (4 flag ON, p95 가드).
  - `test_security_v3.py` — REQ-AGENT-V3-SEC-001 (이중 격리 + truncation).
- 기존 `tests/test_agent_v2/` 전 스위트가 4 all-off 에서 무변경 통과 (회귀 게이트).

### Task 7 — 롤아웃 runbook + cross-SPEC followup [P1]

- `plan.md` 운영 runbook (아래 §3).
- followup PR 메모 (모두 non-blocking-for-run, V3 plan-audit 통과 후 별도 PR):
  1. SPEC-AGENT-V2-REACT spec.md OQ-7 → "resolved by SPEC-AGENT-V3-REACT (evaluator in-loop wrapping)" 표기.
  2. **SPEC-AGENT-V2-CLEANUP-001 RC8 조정 계약 — 단순 "헬퍼 보존" 으로는 불충분.** cleanup SPEC 의 "Env vars to deprecate" 가 `SELF_CRITIQUE_*` family 전체 + `AGENT_V2_REACT_ENABLED` 를 deprecate 하고 `evaluator.py` 모듈을 삭제 대상으로 둔다. V3 Gap2 는 다음 **3가지를 모두** live dependency 화하므로 cleanup followup 은 이를 전부 보존해야 한다: (a) evaluator 평가 헬퍼 모듈 (`evaluator._call_llm`/`_build_fastpath_delta`, `_evaluator_prompt`, `_evaluator_models.CritiqueScore`) — 그래프 노드 wiring 만 제거; (b) **`SELF_CRITIQUE_*` env family 전체** (`SELF_CRITIQUE_MAX_ITERATIONS`/`THRESHOLD`/`TIMEOUT_S`) — Reflexion 재시도 cap·임계·타임아웃; (c) **`EVALUATOR_*` env family 전체** (`EVALUATOR_MODEL`/`MAX_TOKENS`/`TEMPERATURE`/`TIMEOUT_S`) — `_call_llm` 직접 참조. cleanup SPEC 의 `AGENT_V2_REACT_ENABLED` 무조건화는 V3 master→sub-flag 게이트에 무해 (마스터 항상 ON 이어도 4 sub-flag 가 각 갭 독립 게이트, all-off byte-identical 그대로 성립).

---

## 2. sub-flag 별 독립 구현·롤백 매트릭스

| Gap | flag | 구현 격리 | 롤백 (1회) | OFF 시 byte-identical 증거 |
|---|---|---|---|---|
| 1 메모리 주입 | `AGENT_V3_MEMORY_INJECTION_ENABLED` | `_memory_context.py` + react_loop messages 조립 1지점 | flag false + 컨테이너 재시작 | `test_byte_identical` messages 스냅샷 |
| 2 Reflexion | `AGENT_V3_REFLEXION_ENABLED` | `_reflexion.py` + react_loop dispatch-후 훅 1지점 | flag false | ToolMessage content 스냅샷 = V2 |
| 3 능동 제안 | `AGENT_V3_PROACTIVE_ENABLED` | `suggest_next_step.py` + REGISTRY flag 분기 + `_PROACTIVE_DIRECTIVE` | flag false | `TOOL_NAMES` 길이 7 + system msg 스냅샷 |
| 4 dislike | `AGENT_V3_DISLIKE_MEMORY_ENABLED` | TasteProfile additive 필드 + 4 dispatch 의 flag-gated 블록 | flag false | exclude 입력 + TasteProfile 필드집합 스냅샷 |

각 flag 는 직교 경로 (Gap1=system msg, Gap2=ToolMessage, Gap3=REGISTRY+prompt, Gap4=store+exclude). 부분 ON 조합 상호작용 위험은 R9 — pairwise smoke (Gap2+Gap4, Gap1+Gap3) 1개씩 추가.

---

## 3. 운영 롤아웃 Runbook

1. Task 1-6 머지 (4 flag 기본 false → prod 무영향, byte-identical V2).
2. dev 에서 flag 1개씩 ON → dev 봇 수동 스모크 (acceptance.md 시나리오).
3. prod 단계적: Gap1 → Gap4 → Gap3 → Gap2 순 (위험 낮은 순; Gap2 가 latency 영향 최대라 마지막). 각 단계 24h 모니터 (`tool_call` 이벤트 분포, p95 latency).
4. 이상 시 해당 flag 단독 false + 재시작 (다른 Gap 영향 없음 — 직교).
5. 전 갭 안정 후 SPEC-AGENT-V2-REACT OQ-7 resolved followup + SPEC-AGENT-V2-CLEANUP-001 RC8 조정.

---

## 4. 위험 분석 (구현 관점)

| Risk | 구현 완화 |
|---|---|
| R4 (REGISTRY 7→8 byte-identical) | REGISTRY 를 모듈 로드 시점 flag 분기 (OQ-V3-6). settings 는 lifespan 고정이라 race 없음. `test_byte_identical` 가 `TOOL_NAMES` 길이 + 항목 정확 검증. |
| R5 (TasteProfile 직렬화) | additive `field(default_factory=dict)`. `taste_profile_pg` 의 `_jsonable` cascade 통과 단위 테스트 + 구-row 역직렬화 테스트. |
| R3 (Reflexion overrun) | **잔여-budget timeout wrap (normative, REQ-AGENT-V3-REFLEX-DEADLINE-001)**: `remaining = max(0.0, turn_deadline - now)`, `await asyncio.wait_for(evaluate_search_quality(...), timeout=remaining)` — overrun 시 강제 취소. pre-check 만으로는 불충분 (`EVALUATOR_TIMEOUT_S`=8s > `remaining`). slow-evaluator stub 으로 잔여-budget 경계 취소 + turn ≤ turn_deadline 단언. 추가로 `SELF_CRITIQUE_MAX_ITERATIONS` 호출 cap. |
| R1 (token budget) | `build_memory_context` 가 `max_tokens` cap 강제. 주입 후 첫 LLM call 토큰을 단위 perf 테스트로 측정. |
| R7/RC8 (cleanup 충돌) | `_reflexion.py` 가 evaluator 헬퍼 import 를 명시적 live dependency 로 docstring 문서화. cleanup SPEC followup 메모. |

---

## 5. 기존 헬퍼 래핑 지점 (재구현 금지 — research.md §2 ground truth)

| Gap | 래핑 대상 (절대 재구현 금지) |
|---|---|
| 1 | `taste_profile.get_taste_store().get_or_create`, `TasteProfile.boost_brands/boost_keywords/exclude_brands`, `tools.get_recent_history.dispatch` |
| 2 | `evaluator._call_llm`, `evaluator._build_fastpath_delta`, `_evaluator_prompt.build_user_prompt`/`SYSTEM_PROMPT`, `_evaluator_models.CritiqueScore`, env `SELF_CRITIQUE_MAX_ITERATIONS`/`EVALUATOR_*` |
| 3 | `_adapter_ctx.get_adapter`, `adapter.send_text_with_buttons` (ask_user_clarification.py 패턴) |
| 4 | `TasteProfile.reinforce_disliked_*`/`exclude_brands`/`_cap`, `taste_profile_pg` `_jsonable` cascade, 기존 search exclude 경로 (`AnalyzedItem`/`exclude_keywords`) |

---

## 6. MX 태그 계획

- `_memory_context.py` / `_reflexion.py`: 모듈 docstring `@MX:NOTE [AUTO]` + `@MX:SPEC: SPEC-AGENT-V3-REACT` (wrap-only 명시).
- `react_loop.py` Gap2 주입 지점: `@MX:WARN` + `@MX:REASON` (Reflexion 은 `asyncio.wait_for(timeout=remaining turn budget)` 로 강제 wrap — pre-check insufficient; + SELF_CRITIQUE_MAX_ITERATIONS 호출 cap invariant).
- `react_loop.py::run_react_loop` 기존 `@MX:ANCHOR` 유지 — fan_in 변화 없음 (V3 는 본문 in-place 확장).
- `TasteProfile` 신규 필드 + `recency_weighted_excludes`: `@MX:SPEC` + `@MX:NOTE [AUTO] additive, no schema-break`.
- `tool_registry.py` flag 분기: `@MX:NOTE [AUTO] flag-aware 8th tool — byte-identical when OFF`.
- code_comments 설정 확인 후 태그 언어 결정 (`.moai/config/sections/language.yaml`).

---

## 7. Pre-submission Self-Review 게이트

전체 diff 가 SPEC acceptance 충족하는지 + 더 단순한 접근 없는지 검토. 특히: 4개 flag-gated 블록이 모두 "기존 헬퍼 호출 + flag 분기" 형태인지 (새 알고리즘 0). byte-identical 가드가 6 시나리오 전부 cover 하는지.
