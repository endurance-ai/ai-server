# SPEC-AGENT-V3-REACT — Progress

| Field | Value |
|---|---|
| Started | 2026-05-17 (Phase 1 Analysis & Planning) |
| Harness level | standard |
| Development mode | DDD (ANALYZE-PRESERVE-IMPROVE) |
| Scale mode | medium-large — 9 source files (8 modify + 3 new) + 1 NEW Alembic migration + ~7 test files; 2 domains (agent-loop/agentic, memory/persistence); sub-agent sequential DDD (NOT team — changes tightly coupled to byte-identical invariant in shared react_loop.py) |
| conversation_language | Korean (narrative) / English (technical) |
| Phase 1 (Analysis & Planning) | COMPLETE — 2026-05-17 |
| Phase 1.5 (Task Decomposition) | COMPLETE — 2026-05-17 (10 tasks, tasks.md written) |
| Phase 2 (Run) | NOT STARTED — awaiting approval |

## Phase 1 산출물

- 전 SPEC 문서 (spec/plan/acceptance/research/spec-compact) + 프로젝트 컨벤션 (structure/tech) 정독.
- research.md 의 모든 코드 경로·시그니처를 live 코드 직접 대조 검증 (2026-05-17, `feature/SPEC-AGENT-V2-REACT` 브랜치).
- 18 REQ → 10 task 매핑. byte-identical-off 가드를 첫 갭 코드 전(T2)에 testable 로 시퀀싱.
- 1건의 material SPEC 부정확성 발견 + run-blocking ambiguity 1건 식별 (Gap4 DB 마이그레이션 — 아래).

## 검증 결과: research.md 정확도

~95% 정확. react_loop 주입 지점 2곳 (L305-308, L556-596), `turn_deadline`(L299), evaluator 헬퍼, TasteProfile dataclass/Protocol, config env 블록, tool_registry REGISTRY 구조, get_recent_history dispatch 모두 live 코드와 일치.

## ⚠ Material 부정확성 (spec.md/plan.md/spec-compact.md 공통)

**Gap4 PG 영속화는 `_jsonable` cascade 가 아니다.** `taste_profile_pg.py` 는 `_jsonable` 을 전혀 사용하지 않음 (검증: `grep -rn _jsonable app/channels/taste_profile*` → 0건). 명시적 컬럼 INSERT/UPSERT (`Jsonb()` 파라미터, L113-141) + 명시적 SELECT 컬럼 목록 (L95-97) + `_row_to_profile` 위치 기반 tuple unpack (L184-204) 사용. 따라서 Gap4 는 추가로:
1. **NEW Alembic 마이그레이션** (`migrations/versions/0006_add_taste_dislike_ts.py`) — `ai.user_taste_profile` 에 2 JSONB 컬럼 ADD. (프로젝트는 Alembic 사용 — `migrations/versions/`, `env.py`, `test_migration_0004.py` 선례.)
2. `_aupdate`/`_aget_or_create`/`_row_to_profile` 명시적 SQL·tuple unpack 3곳 수정.

→ tasks.md T9 가 이를 (b)/(c) 서브스텝으로 반영. RC5 는 "default_factory 가 처리" 로 과소평가 — 미-마이그레이션 DB 의 명시적 SELECT 는 hard fail (graceful empty dict 아님). **배포 순서: 마이그레이션 선행 후 코드.** = 유일한 run-blocking ambiguity (report 참조).

## Iteration Log

| # | AC met (count) | error delta | note |
|---|---|---|---|
| (Phase 1) | — | — | Analysis & planning only; no code. |
| T1 | env infra | 0 | 5 env added after AGENT_V2 block; .env.example + test_config_v3 (9 pass). Drift: planned=actual. |
| T2 | P0 guard testable | 0 | byte-identical guard + V2 baseline established with ZERO gap code; 167 pass (test_agent_v3 + test_agent_v2). P0 invariant confirmed against untouched V2. Drift: planned=actual. |
| T3 | AC-1.1/1.2/1.3/1.4 | 0 | Gap1 _memory_context.py NEW + react_loop messages flag-branch. Fail-soft, token-cap newest-first, flag-OFF byte-identical + 0 memory-path calls. Drift: planned=actual. |
| T4 | AC-S.1 | 0 | Security: 200-cap reuse + dual fence + block≤cap*4 verified. Test-only. Drift: planned=actual. |
| T5 | AC-2.2 | 0 | Gap2 _reflexion.py NEW — wraps _call_llm/_build_fastpath_delta/build_user_prompt; fail-open propagated; cross-SPEC RC8 docstring. Drift: planned=actual. |
| T6 | AC-2.1/2.3/2.4 | 0 | Gap2 in-loop wiring + bound (SELF_CRITIQUE_MAX_ITERATIONS cap); history/iteration untouched; flag-OFF byte-identical ToolMessage. Drift: planned=actual. |
| T7 | AC-2.5/2.6 | 0 | Residual-budget asyncio.wait_for forced-cancel — slow stub cancelled at residual boundary (0.33s, NOT 8s/20s); zero+positive control. Combined into _maybe_reflexion helper with T6 (two REQ groups, independently asserted). Drift: planned=actual. |
| T8 | AC-3.1/3.2/3.3 | 0 | Gap3 suggest_next_step.py NEW (≤80 LOC) + tool_registry flag-aware 8th entry (module-load, OQ-V3-6) + _PROACTIVE_DIRECTIVE. flag-OFF 7-tool + prompt byte-identical. Drift: +_rebuild_registry_for_flag test helper (rationale below). |
| T9 | AC-4.1/4.2/4.3/E6 | 0 | Gap4 additive ts dicts + recency_weighted_excludes (reuses exclude_brands) + migration 0005 + pg 3-SQL edits + update_taste/search/refine flag-gated. flag-OFF byte-identical. Migration tests Docker-skipped (precedent: test_migration_0004). Drift: filename 0005 (corrected from tasks.md 0006). |
| T10 | AC-X.2/P.2/E1-E6 | 0 | wrap-only AST + perf (assembly<50ms, slow-eval cancel) + orthogonality E1-E6. Final full suite: 789 pass / 9 fail (all 9 pre-existing baseline, .env-driven, out of scope — net regression 0). behavior_preserved=true. Drift: planned=actual. |

## Phase 2 Run Log

- **2026-05-17 — Material divergence (factual correction, not OQ re-open):** tasks.md T9 hardcodes migration filename `0006_add_taste_dislike_ts.py`, but `migrations/versions/` latest is `0004` → next sequential is **0005**. Spawn instruction is authoritative ("next sequential version after the latest in migrations/versions/"). Using `0005_add_taste_dislike_ts.py` + `test_migration_0005.py`. Documented; not a guess.

### implementation_divergence

- **planned_files**: all planned files implemented as specced.
- **actual_files**: identical to planned EXCEPT migration filename `0005_*` (planned `0006_*` — corrected per authoritative "next sequential" instruction; latest existing is 0004).
- **additional_features**: (1) `tool_registry._rebuild_registry_for_flag(enabled)` — test-only helper. Rationale: Gap3 registry is built once at module-load (OQ-V3-6 resolved); tests that flip `AGENT_V3_PROACTIVE_ENABLED` post-import need a deterministic way to mirror a fresh-boot REGISTRY without process restart. Production path unchanged (module-load branch). (2) `_v2_baseline.V2_TASTE_PROFILE_FIELD_REPRS` — frozen baseline constant added for the superset-not-equality field assertion (SPEC-mandated additive schema; AC-4.3 / REQ-AGENT-V3-DISLIKE-SCHEMA-001 acceptance explicitly defines "V2 superset, exactly new ts dicts added").
- **scope_changes**: none. No file outside the SPEC planned set was modified. fashion_bot.py / state.py WorkingState / evaluator.py body / conversation_log.py / TasteProfile Protocol·update()·reinforce_*·exclude_brands·_cap = UNCHANGED (AST/snapshot verified by test_wrap_only + test_byte_identical).
- **new_dependencies**: none.
- **new_directories**: `tests/test_agent_v3/` (planned).
- **T6/T7 structural note**: T6 (bound) + T7 (deadline wait_for) implemented in one helper `_maybe_reflexion`. They remain two distinct, independently-asserted REQ groups (test_gap2_reflexion_bound vs test_gap2_reflexion_deadline). T7's deadline-cancel is normative and mechanically proven (slow stub cancelled at residual boundary, not EVALUATOR_TIMEOUT_S).

### byte-identical / behavior-preservation

- T2 byte-identical guard established BEFORE any gap code (167 pass, 0 gap LOC) — P0 invariant testable pre-gap, as required. Re-verified green after every gap (T3,T6,T8,T9).
- Final full suite: **789 passed, 9 failed, 117 skipped**. All 9 failures confirmed PRE-EXISTING on the stashed baseline (zero V3 code → identical 9 failures): `.env`-driven (`ONBOARDING_CARDS_ENABLED=false`, `AGENT_V2_REACT_ENABLED=true`) in `test_graph_flows`/`test_routing_onboarding`/`test_graph_topology`/`test_config_validators` — entirely outside SPEC-AGENT-V3-REACT scope. **Net regression introduced = 0. behavior_preserved = true.**
- `tests/test_memory_pg/test_migration_0005.py` (4 tests) Docker-SKIPPED locally (testcontainers unavailable) — same skip behavior as the established `test_migration_0004` precedent. Non-DB serialization backward-compat (old short tuple, new tuple, json.dumps) IS covered without Docker in `test_gap4_dislike::test_ac_4_1_serialization_compat_and_old_row_load`. Migration code mirrors verified 0004 pattern (additive `IF NOT EXISTS`, default `'{}'::jsonb`).
- **Gap4 deploy order (SPEC ambiguity, documented per instruction):** migration-FIRST then code. The explicit-column SELECT in `taste_profile_pg._aget_or_create` (now `RETURNING ... disliked_brands_ts, disliked_keywords_ts`) requires the columns to exist; an un-migrated DB would hard-fail. Migration 0005 default `'{}'::jsonb` + `_row_to_profile` short-tuple fallback are the backward-compat guarantees. NOT applied to any live DB.

## Phase 2.8a — Targeted fix-evaluate cycle 1/3 (evaluator FAIL 0.746 → resolved)

- **BLOCKING-1 (test isolation, P0)** — FIXED. Added `tests/test_agent_v3/conftest.py::_v3_isolation` (autouse, function-scoped): clears `get_settings` lru_cache + defensively rebinds a V3-aware live `settings` to all importer modules; snapshots+restores 13 guarded settings attrs around every test (zero cross-test/cross-suite bleed); centrally resets the taste-store singleton + flag-aware tool REGISTRY to V2 baseline. Removed the now-redundant per-file `_reset_taste_store`/`_reset` autouse fixtures from 5 V3 test files (DRY — single isolation source). Edge-orthogonality kept a slimmed `_edge_baseline` (only its non-redundant START-state setup). Full `uv run pytest` → **9 failed, 810 passed, 117 skipped**, ZERO V3 ERROR/FAIL. The 9 are byte-identical the pre-existing baseline set (stash-verified cycle 0).
- **BLOCKING-2 (coverage <85%)** — FIXED. Added error-path/branch tests ONLY (zero production change): suggest_next_step ×5 (invalid_kind/missing opts/missing chat_id/send_text fallback/send-raises), _memory_context ×4 (taste-store fail-soft/disliked_brands line/recent-history fail-soft/hard-truncation fallback), _reflexion ×1 (helper-import fail-open), update_taste ×4 (invalid_source/missing_user_key/keyword likes+dislikes+ts/store-exception), apply_dislike_discount ×4 + _cap ts-overflow ×1 + search_products.dispatch real path ×1 + refine_search.dispatch real path ×1. Result: every V3 NEW/CHANGED line = **100%** covered. Per-file: _memory_context 100%, _reflexion 100%, suggest_next_step 100%, update_taste 100%. taste_profile/search_products/refine_search per-file aggregate is lower ONLY due to large pre-existing untouched V2 bodies (DDD: unchanged code not re-tested); the SPEC-changed line set in each = 100%.
- **BLOCKING-3 (wrap-only AST under full suite)** — VERIFIED. `test_wrap_only.py` + `test_gap4_reuses_exclude_brands_no_new_ranking` + `test_gap4_search_discount_reuses_filter_pattern` pass under cross-suite ordering (resolved automatically once BLOCKING-1 isolation landed).
- **Production code delta this cycle = 0.** All fixes are test-infra (new conftest) + test additions/assertion corrections. The 8 changed production files are unchanged from cycle 0. Working tree clean (0 stash entries, no git ops, branch `feature/SPEC-AGENT-V3-REACT`).
