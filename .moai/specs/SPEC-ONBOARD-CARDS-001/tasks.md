---
id: SPEC-ONBOARD-CARDS-001
tasks_version: 0.1.0
spec_version: 0.3.2
plan_version: 0.1.0
created: 2026-05-15
methodology: DDD (ANALYZE-PRESERVE-IMPROVE)
total_tasks: 22
---

# Task Decomposition — SPEC-ONBOARD-CARDS-001

> **Methodology**: DDD (ANALYZE-PRESERVE-IMPROVE). 본 SPEC은 기존 12 graph 노드를 **건드리지 않는다** (REQ-ONBOARD-GRAPH-001) — 따라서 PRESERVE 범위가 좁다. 대신 손대는 *기존* 파일 9개 (`fashion_bot.py`, `routing.py`, `state.py`, `session.py`, `session_pg.py`, `taste_profile.py`, `taste_profile_pg.py`, `link_resolver.py`, `webhooks/telegram.py`)는 각각 characterization 또는 regression test 선행 후 IMPROVE. 신규 모듈 9개는 greenfield.
>
> **Complexity legend**: S = < 1h, M = 2-4h, L = full day (~6-8h).
>
> **AC links**: 각 task의 `REQ` 컬럼은 SPEC §Requirements 의 REQ-ONBOARD-* ID 참조. plan.md §11.4 가 13 manual scenarios (a)–(m) → test file 매핑.
>
> **Cross-SPEC**: REQ-ONBOARD-MEMORY-AMEND-001 의 SPEC-MEMORY-001 v1.1.0 amendment 는 commit `0c59e8b` 로 already land — pre-merge gate 통과.

---

## Phase 1 — Foundation (no graph nodes touched)

새 모듈을 isolation에서 빌드. characterization test 불필요 (greenfield). 일부는 **parallel 가능** (의존성 표 참고).

### ONB-T01 — Alembic migration `0004_add_onboarded_at.py`

- **Description**: SPEC REQ-ONBOARD-MIGRATION-001 + REQ-ONBOARD-PINTEREST-007 cache columns. 7개 컬럼 한 번에 추가 (`onboarded_at` + 3 onboard_state + 3 Pinterest cache). `ALTER TABLE IF NOT EXISTS` (idempotent). 백필: 기존 row 전부 `onboarded_at = now()`. `downgrade()` 는 7개 컬럼 모두 drop. plan §1.
- **Files**:
  - CREATE: `migrations/versions/0004_add_onboarded_at.py`
- **REQ**: REQ-ONBOARD-MIGRATION-001, REQ-ONBOARD-PINTEREST-007
- **Complexity**: S
- **Predecessors**: (none — foundation)
- **First action**: `ls migrations/versions/` 재확인 — `0004` 미점유 검증. 점유 시 `0005`로 리넘버링 + `down_revision`.
- **Verification**: `alembic upgrade head` + `alembic downgrade -1` on testcontainers PG. `\d ai.user_session` 스냅샷 비교 (existing + 7 new columns).

### ONB-T02 — `app/channels/onboarding_values.py`

- **Description**: SPEC §"카드 옵션 카탈로그" 표 1:1 매핑. 3 `OnboardingOption` 리스트 (`MOOD_OPTIONS` 8개, `COLOR_OPTIONS` 6개, `FIT_OPTIONS` 4개), `STAGE_BOUNDS` dict, KO/EN intro line tables (`INTRO_LINES_KO`, `INTRO_LINES_EN`), confirmation card 문구 상수, `keywords_to_boost` 1..5 검증. plan §2.1.
- **Files**:
  - CREATE: `app/channels/onboarding_values.py`
- **REQ**: REQ-ONBOARD-CARDS-001, REQ-ONBOARD-LANG-002
- **Complexity**: M
- **Predecessors**: (none)
- **Verification**: `tests/test_onboarding/test_onboarding_cards.py::test_option_catalog_shape_and_uniqueness` — 8/6/4 length, value uniqueness per stage, label ≤ 16 chars, kw 1..5 all-lowercase.

### ONB-T03 — `app/channels/pinterest_url.py` classifier

- **Description**: `classify_pinterest_input(text, *, max_pins=20) -> PinInput` 함수 + 4-way tagged union (`Pins`/`Board`/`Profile`/`_None`). Host: `urllib.parse.urlsplit` 으로 host 추출 후 canonical regex `^([a-z]{2}\.)?pinterest\.com$|^pin\.it$|^www\.pinterest\.com$`. 우선순위 PIN > BOARD > PROFILE. 20-pin cap (truncated flag). Scheme normalization (`http://` → reject; bare → `https://`). plan §3.1.
- **Files**:
  - CREATE: `app/channels/pinterest_url.py`
- **REQ**: REQ-ONBOARD-PINTEREST-002, REQ-ONBOARD-SEC-001
- **Complexity**: M
- **Predecessors**: (none)
- **Verification**: `tests/test_onboarding/test_pinterest_classify.py` — 25+ fixtures (plan §3.1 matrix) + 20 attack URL set (javascript:, IDN, `pinterest.com.evil.com`).

### ONB-T04 — `app/providers/apify.py` async wrapper

- **Description**: `ApifyProvider` class + `run_pinterest_scrape(url, *, mode, max_items, timeout_s) -> list[PinResult]`. `apify-client>=1.7.0` Python SDK 채택 (`pyproject.toml` main deps 추가). graceful degrade — `asyncio.TimeoutError`, `ApifyApiError`, empty, missing token 모두 빈 list 반환 (NEVER raises). `_safe_log_url` 로 PII redact. SSRF guard reuse via `_has_valid_image`. plan §3.2.
- **Files**:
  - CREATE: `app/providers/apify.py`
  - MODIFY: `pyproject.toml` (deps: `apify-client>=1.7.0,<2`)
- **REQ**: REQ-ONBOARD-PINTEREST-005, REQ-ONBOARD-SEC-001
- **Complexity**: M
- **Predecessors**: (none)
- **First action (probe)**: `apify-client` 로 `epctex/pinterest-scraper` actor input schema + profile/board mode 둘 다 지원 확인 (small live probe with throwaway board URL — 또는 actor docs 검증). `pin.it` 처리도 함께 확인. 결과는 ONB-T15 onboard_pinterest 노드 구현 입력.
- **Verification**: `tests/test_onboarding/test_apify_provider.py` — mocked actor success/timeout/empty/401/missing-token paths + log no URL leak.

### ONB-T05 — `TasteProfileStore.seed_from_onboarding` Protocol + 2 backends

- **Description**: Protocol 메서드 추가 (`async def seed_from_onboarding(user_key, *, keyword_weights, brand_weights=None) -> None`) + `InMemoryTasteProfileStore` + `PostgresTasteProfileStore` 구현. 둘 다 `lock_for(user_key)` async lock 하에 load → per-keyword `reinforce_liked_keywords([kw], weight=w)` + per-brand `reinforce_liked_brand(br, weight=w)` → persist. **Additive merge only**, NEVER overwrite. plan §4.
- **Files**:
  - MODIFY: `app/channels/taste_profile.py` (Protocol + InMemory 구현)
  - MODIFY: `app/channels/taste_profile_pg.py` (Postgres 구현)
- **REQ**: REQ-ONBOARD-SEED-001, REQ-ONBOARD-MEMORY-AMEND-001
- **Complexity**: M
- **Predecessors**: (none — Protocol amendment commit `0c59e8b` is already land)
- **First action**: `git log --oneline -- .moai/specs/SPEC-MEMORY-001/spec.md | head -3` 로 amendment commit 확인.
- **Characterization (DDD PRESERVE)**: existing `tests/test_memory_pg/test_session_store.py` + `test_taste_store.py` 전체 green — additive-only이라 기존 메서드 동작 무변경.
- **Verification**: `tests/test_onboarding/test_taste_seed.py` — InMemory + Postgres backend 각각 additive merge against pre-existing weights, decay applied to old, additive for new.

### ONB-T06 — `link_resolver.resolve_batch(urls, concurrency=5)` extension

- **Description**: 기존 `app/channels/link_resolver.py` 끝에 `async def resolve_batch(urls, *, concurrency=5) -> list[str]` 추가. `asyncio.gather` + `Semaphore`, 단일-URL `resolve()` 의 캐시/SSRF/redirect/Pinterest originals 정책 그대로 재사용. 실패한 URL은 결과에서 omit (예외 X). plan §3.4.
- **Files**:
  - MODIFY: `app/channels/link_resolver.py`
- **REQ**: REQ-ONBOARD-PINTEREST-005 (mode C)
- **Complexity**: S
- **Predecessors**: (none)
- **Characterization (DDD PRESERVE)**: `tests/test_channels/test_link_resolver_characterization.py::test_resolve_single_url_unchanged_after_batch_addition` — 5 fixtures (Pinterest pin, IG returns [], http→https redirect, og:image extract, cache hit) pre/post-SPEC identical.
- **Verification**: `tests/test_onboarding/test_link_resolver_batch.py` — concurrency cap, failure omission, mixed success/fail, cache reuse, 20-pin batch.

### ONB-T07 — `app/core/config.py` env vars + weight validation

- **Description**: 11 new env vars per SPEC §Environment Variables (plan §4.4 validation). `model_validator(mode="after")` 로 weight range validation (WARN-only, no crash). `BOT_DEFAULT_LANG` 추가. plan §10.2 #3 `apify-client` dep.
- **Files**:
  - MODIFY: `app/core/config.py`
  - MODIFY: `.env.example`
- **REQ**: REQ-ONBOARD-SEED-002, plus all env-table vars (SPEC §Environment Variables)
- **Complexity**: S
- **Predecessors**: (none)
- **Verification**: `tests/test_core/test_config_onboarding_weights.py` — default range pass + out-of-range → WARN log + no crash.

---

## Phase 2 — Card Builders & Shared Helpers (no graph nodes yet)

### ONB-T08 — `app/channels/onboarding_cards.py` builders

- **Description**: 5 builders: `build_mood_card`, `build_color_card`, `build_fit_card`, `build_pinterest_card`, `build_restart_confirmation_card`. 각각 `(text, list[list[InlineKeyboardButton]])` 반환. Selected toggle marker `"✓ "` prefix. `parse_onboard_callback(callback_data)` 엄격 파서. Callback format `onboard:{stage}:{action}:{value?}` 64-byte 안전 (plan §2.2 table). plan §2.
- **Files**:
  - CREATE: `app/channels/onboarding_cards.py`
- **REQ**: REQ-ONBOARD-CARDS-001, REQ-ONBOARD-CARDS-002, REQ-ONBOARD-CARDS-003, REQ-ONBOARD-LANG-001
- **Complexity**: M
- **Predecessors**: ONB-T02 (option catalog)
- **Verification**: `tests/test_onboarding/test_onboarding_cards.py` (이미 ONB-T02에서 시작 — confirm builder snapshot pass).

### ONB-T09 — Shared `_pinterest_helpers` module

- **Description**: `app/graphs/nodes/_pinterest_helpers.py` 신규 — (a) `aggregate_pin_weights(image_urls, *, pin_weight, concurrency=5)` Vision batch aggregator with per-pin failure isolation, (b) `_check_pinterest_cache(sess, normalized_url, ttl_s) -> _AggregatedWeights | None` 24h cache check, (c) `_normalize_pinterest_url(url) -> str` host lowercase + strip trailing `/` + strip query, (d) `ingest_pinterest_pins(state, classifier_result, *, apify_provider, session_store, taste_store, continuous_origin) -> _IngestOutcome` 공용 ingest 파이프라인. plan §3.5, §3.6, §3.7.
- **Files**:
  - CREATE: `app/graphs/nodes/_pinterest_helpers.py`
- **REQ**: REQ-ONBOARD-PINTEREST-006, REQ-ONBOARD-PINTEREST-007 (cache check), REQ-ONBOARD-PINTEREST-003 (continuous_origin branch)
- **Complexity**: L (single seed call discipline 분기 + cache check/write + Vision aggregation + per-pin failure isolation)
- **Predecessors**: ONB-T03 (classifier), ONB-T04 (apify provider), ONB-T05 (`seed_from_onboarding`), ONB-T06 (`resolve_batch`)
- **Verification**: `tests/test_onboarding/test_pinterest_ingest.py` 일부 (cache hit/miss, Vision aggregation, single seed call discipline branch).

### ONB-T10 — `_complete_onboarding` helper

- **Description**: `app/graphs/nodes/_onboard_helpers.py::complete_onboarding(state)`. Card-derived weights 계산 (mood/color/fit options.keywords_to_boost × `ONBOARDING_CARD_SEED_WEIGHT`) + `state.onboard_pin_weights` 와 union merge (overlap → sum) → **단 1회** `taste_store.seed_from_onboarding(...)` 호출 → `sess.onboarded_at = now()` + persist → completion message (Pinterest success vs degraded variant) → `state.onboard_stage="done"` + `state.onboard_pin_weights=None`. Order 3-before-4 (seed before mark) for crash-safety. plan §6.6.
- **Files**:
  - CREATE: `app/graphs/nodes/_onboard_helpers.py`
- **REQ**: REQ-ONBOARD-COMPLETION-001
- **Complexity**: M
- **Predecessors**: ONB-T05, ONB-T09
- **Verification**: `tests/test_onboarding/test_completion_flow.py::test_pinterest_success_makes_exactly_one_seed_call` + `test_completion_flow.py::test_pinterest_skip_card_only_seed`.

---

## Phase 3 — Graph Nodes (5 onboarding + 1 continuous = 6 new nodes)

각 node는 thin async function. **noop** for existing 12 nodes (REQ-ONBOARD-GRAPH-001 AC). 신규 노드 6개는 greenfield — characterization 불필요.

### ONB-T11 — State extension (`WorkingState` + `Session`)

- **Description**: `app/graphs/state.py::WorkingState` 5 new fields (`onboard_stage`, `onboard_selections`, `onboard_card_message_id`, `continuous_origin`, `onboard_pin_weights`). `app/channels/session.py::Session` 7 new fields (matching plan §5.3). `app/channels/session_pg.py` 의 `_from_db_row` / `_to_jsonable` 7개 새 컬럼 매핑. plan §5.
- **Files**:
  - MODIFY: `app/graphs/state.py`
  - MODIFY: `app/channels/session.py`
  - MODIFY: `app/channels/session_pg.py`
- **REQ**: REQ-ONBOARD-MIGRATION-002, REQ-ONBOARD-GRAPH-002
- **Complexity**: M
- **Predecessors**: ONB-T01 (migration)
- **Characterization (DDD PRESERVE)**: existing `tests/test_graph_state.py`, `tests/test_channels/test_session.py`, `tests/test_memory_pg/test_session_store.py` 전체 green (Pydantic `extra="forbid"` 호환, dataclass default-None backward-compat).
- **Verification**: `tests/test_memory_pg/test_session_store_onboarding_columns.py` (NEW) — Session round-trip with all 7 new fields populated/empty.

### ONB-T12 — `onboard_intro` node

- **Description**: `app/graphs/nodes/onboard_intro.py`. 두 사용 케이스: (a) fresh `onboarded_at IS NULL` 또는 explicit re-trigger → 3-line greeting + 3-line usage guide + Stage 1 mood card (single webhook turn). (b) returning user + `/start` 텍스트 → "다시 시작할까요?" confirmation card 만. sticky lang via `session_lang(sess)`. Langfuse span `onboarding.intro` (metadata `lang`, `is_restart_attempt`). plan §6.5, §7.1.
- **Files**:
  - CREATE: `app/graphs/nodes/onboard_intro.py`
- **REQ**: REQ-ONBOARD-ENTRY-001, REQ-ONBOARD-ENTRY-002, REQ-ONBOARD-ENTRY-003, REQ-ONBOARD-LANG-001, REQ-ONBOARD-LANG-002, REQ-ONBOARD-OBS-001
- **Complexity**: M
- **Predecessors**: ONB-T08 (card builders), ONB-T11 (state)
- **Verification**: `tests/test_onboarding/test_onboard_nodes.py::{test_fresh_user_intro_kicks_stage_1, test_returning_user_sees_confirmation_only, test_no_callback_yes_path_re_enters_flow}`.

### ONB-T13 — `onboard_mood` + `onboard_color` + `onboard_fit` nodes

- **Description**: 3 stage 노드 (mood/color/fit). 공통 패턴: toggle callback → `state.onboard_selections[stage]` 갱신 + `editMessageReplyMarkup` re-render. `next` callback → bounds check (table from `STAGE_BOUNDS`) → 통과 시 advance, 실패 시 toast "{min}~{max}개 선택해 주세요". `skip` callback → empty selection 으로 advance. Sticky lang. Each emits `onboarding.stage.{mood|color|fit}` Langfuse span + `onboard_select` event (SPEC-CONVERSATION-LOG-001 catalog activation). plan §6.5.
- **Files**:
  - CREATE: `app/graphs/nodes/onboard_mood.py`
  - CREATE: `app/graphs/nodes/onboard_color.py`
  - CREATE: `app/graphs/nodes/onboard_fit.py`
- **REQ**: REQ-ONBOARD-CARDS-001, REQ-ONBOARD-CARDS-002, REQ-ONBOARD-CARDS-003, REQ-ONBOARD-LANG-001, REQ-ONBOARD-OBS-001
- **Complexity**: L (3 노드, 공통 로직 추출 가능 but 각 노드 독립 테스트)
- **Predecessors**: ONB-T11, ONB-T12
- **Verification**: `test_onboard_nodes.py::{test_mood_toggle_marks_checkmark, test_mood_next_too_few_toast, test_mood_skip_advances_with_empty, test_mood_max_3_blocked, ...}` — per-stage scenarios + cross-stage advance + edit-vs-send call assertions.

### ONB-T14 — `onboard_fit → END` completion branch (Pinterest disabled / skip)

- **Description**: ONB-T13의 `onboard_fit` 노드가 `PINTEREST_BOOTSTRAP_ENABLED=false` OR Stage 4 skip 경로일 때 `_complete_onboarding(state)` 호출 후 END 도달. 별도 신규 노드가 아닌 `onboard_fit` 내부 분기 — SPEC L660-662 edge 7 (`onboard_fit → END` conditional). plan §6.2.
- **Files**:
  - MODIFY: `app/graphs/nodes/onboard_fit.py` (ONB-T13 작업 분에서 함께 처리)
- **REQ**: REQ-ONBOARD-PINTEREST-001 (`PINTEREST_BOOTSTRAP_ENABLED=false` path), REQ-ONBOARD-COMPLETION-001
- **Complexity**: S (편의상 ONB-T13에 포함 가능)
- **Predecessors**: ONB-T10, ONB-T13
- **Verification**: `test_onboard_nodes.py::test_pinterest_flag_disabled_skips_stage_4` (manual scenario g).

### ONB-T15 — `onboard_pinterest` node (Stage 4)

- **Description**: Stage 4 두 sub-stage: (A) 카드 표시 + URL/skip 버튼 + `AWAITING_PINTEREST_URL` state 진입. (B) text 수신 → `classify_pinterest_input` → `ingest_pinterest_pins(continuous_origin=False)` → `state.onboard_pin_weights` stash → `_complete_onboarding`. Skip 경로 → 즉시 `_complete_onboarding` (card-only seed). 3-strike NONE 자동 skip + degraded path. `APIFY_TOKEN` 없으면 mode A/B 시도 시 degraded message 후 mode C 받기 위해 same state 유지. Langfuse span `onboarding.stage.pinterest`. plan §6.5.
- **Files**:
  - CREATE: `app/graphs/nodes/onboard_pinterest.py`
- **REQ**: REQ-ONBOARD-PINTEREST-001, 002, 004, 005, 006, REQ-ONBOARD-COMPLETION-001, REQ-ONBOARD-LANG-001, REQ-ONBOARD-OBS-001
- **Complexity**: L
- **Predecessors**: ONB-T09, ONB-T10, ONB-T11
- **Verification**: `tests/test_onboarding/test_pinterest_ingest.py::{test_stage4_skip_card_only, test_stage4_pin_urls_mode_c, test_stage4_board_url_apify, test_stage4_no_token_degraded, test_stage4_three_strike_auto_skip, ...}`.

### ONB-T16 — `pinterest_ingest` node (continuous bootstrap)

- **Description**: 온보딩 외 시점 발동. `state.continuous_origin=True`, `ingest_pinterest_pins(continuous_origin=True)` → 직접 `seed_from_onboarding` 호출 (no completion phase). 모드별 confirmation message ("📌 보드/프로필/N개 핀 분석해서 취향에 더했어요"). `sess.onboarded_at` 변경 X. Rate-limit 체크 (5-min window) + 24h cache (mode A/B). Langfuse span `pinterest.continuous_ingest`. plan §3.6, §6.5.
- **Files**:
  - CREATE: `app/graphs/nodes/pinterest_ingest.py`
- **REQ**: REQ-ONBOARD-PINTEREST-003, REQ-ONBOARD-PINTEREST-006 (continuous path), REQ-ONBOARD-PINTEREST-007, REQ-ONBOARD-OBS-001
- **Complexity**: M
- **Predecessors**: ONB-T09, ONB-T15 (shares `ingest_pinterest_pins` helper, structural similarity helps)
- **Verification**: `test_pinterest_ingest.py::{test_continuous_path_no_onboarded_at_mutation, test_rate_limit_within_5_minutes, test_cache_hit_skips_apify, test_continuous_pin_urls_mode_c, test_continuous_board_url_apify, test_continuous_apify_token_unset_mode_c_works}` (manual scenarios k, l, m).

---

## Phase 4 — Topology + Routing + Webhook Entry + Validation

### ONB-T17 — `fashion_bot.py` graph topology (6 nodes + 8 new edges)

- **Description**: `app/graphs/fashion_bot.py::build_graph()` 에 신규 6 노드 등록 + 8 edge 추가 (plan §6.2 table). 기존 12 노드 / 기존 edge 한 글자도 안 건드림 (REQ-ONBOARD-GRAPH-001 AC L671). 정확히 9-entry routing topology (6 inter-node + 3 → END terminators per plan §6.2). plan §6.1, §6.2.
- **Files**:
  - MODIFY: `app/graphs/fashion_bot.py`
- **REQ**: REQ-ONBOARD-GRAPH-001
- **Complexity**: M
- **Predecessors**: ONB-T12 ~ ONB-T16 (모든 노드 land 후)
- **Characterization (DDD PRESERVE)**: `tests/test_graph/test_topology_characterization.py::test_existing_12_nodes_unchanged_after_onboarding_added` — registered node 리스트 diff = exactly 6 additions, 0 removals. 12 existing node 파일 git diff = 0.
- **Verification**: `test_topology_characterization.py::test_18_nodes_9_new_routing_entries` — total nodes 18 (12 existing + 6 new), 9 new routing entries assert against the precise topology, NOT the headline "8 new edges" number.

### ONB-T18 — Routing functions (`onboarding_required`, `is_continuous_pinterest`, `after_onboard_*`)

- **Description**: `app/graphs/routing.py` 에 신규 라우팅 함수 추가:
  - `onboarding_required(state, sess) -> bool` — gate 조건 (plan §6.4)
  - `is_continuous_pinterest(state, sess) -> bool` — rate-limit + URL detect (plan §6.4)
  - `_is_restart_keyword(text)` — exact-match regex (plan §7.2)
  - `after_onboard_mood/color/fit/pinterest` — next/skip/url branches
  - 기존 `_route_after_ingest` 에 onboarding gate 추가 (existing branches 전혀 안 건드림 — onboarding gate가 가장 먼저).
- **Files**:
  - MODIFY: `app/graphs/routing.py`
- **REQ**: REQ-ONBOARD-ENTRY-001, REQ-ONBOARD-ENTRY-002, REQ-ONBOARD-PINTEREST-003, REQ-ONBOARD-GRAPH-001
- **Complexity**: M
- **Predecessors**: ONB-T11 (state fields), ONB-T17 (topology)
- **Characterization (DDD PRESERVE)**: existing `tests/test_graph/test_routing.py` 전체 통과 — non-onboarding flows 변경 X.
- **Verification**: `tests/test_graph/test_routing_onboarding.py` — 12+ scenarios: fresh/restart-kw/restart-callback/continuous-pin/continuous-board/continuous-rate-limited/mid-flow-resume/photo-during-onboarding(ignored)/...

### ONB-T19 — Webhook `/start` parsing + lifespan Apify warmup

- **Description**: `app/api/webhooks/telegram.py` — `/start` 파싱은 routing에서 일반 텍스트로 처리되므로 webhook은 변경 최소. **Optional**: `_extract_callback_source_message_id` 옆에 small no-op `_is_command_text(text)` helper 추가 (logging 명확성). `app/main.py::lifespan` 에 `ApifyProvider.start()` 추가 (env 있을 때만). plan §10 PR-005.
- **Files**:
  - MODIFY: `app/api/webhooks/telegram.py` (minor — `/start` logging clarity only; routing handles dispatch)
  - MODIFY: `app/main.py` (lifespan: Apify provider warmup)
- **REQ**: REQ-ONBOARD-ENTRY-001 (cascade)
- **Complexity**: S
- **Predecessors**: ONB-T04 (Apify provider), ONB-T17, ONB-T18
- **Characterization (DDD PRESERVE)**: existing `tests/test_api/test_webhooks_telegram.py` + `tests/test_conversation_log/test_thread_*.py` 전체 통과 — intake emit shape 무변경, `/start` 텍스트는 그대로 LOG-T08 user_text emit.
- **Verification**: lifespan startup 로그에 `🎨 [APIFY]` 한 줄 (token 있을 때) 또는 degraded 메시지 (token 없을 때).

### ONB-T20 — 13 manual scenarios (a)–(m) automation

- **Description**: SPEC §Definition of Done L997-1010 의 13 시나리오 중 12개 자동화 (plan §11.4 table). Scenario (e) 만 real Apify creds 필요 → dev bot 실측 procedure 로 `docs/infra/deployment.md` 에 기록. 자동화 시나리오는 mocked Apify + Vision + FakeAdapter 패턴 (StubAdapter 가 SPEC-MSG-001 에 이미 존재 — 재사용).
- **Files**:
  - CREATE: `tests/test_onboarding/test_completion_flow.py`
  - CREATE: `tests/test_onboarding/test_onboard_nodes.py` (mood/color/fit/skip/resume/etc.)
  - CREATE: `tests/test_onboarding/test_pinterest_ingest.py` (continuous + cache + rate-limit)
  - CREATE: `tests/test_onboarding/test_pinterest_url_validation.py` (3-strike auto-skip + scheme rejection)
  - UPDATE: `docs/infra/deployment.md` (scenario e manual procedure + cutover order from plan §1.3)
- **REQ**: All P0 REQs (DoD L997-1010)
- **Complexity**: L
- **Predecessors**: ONB-T11..T18
- **Verification**: `pytest tests/test_onboarding/ -v` 전부 green. ≥ 35 test cases (DoD L1012).

### ONB-T21 — Migration + privacy + GIN tests

- **Description**: 추가 보안/마이그레이션 검증:
  - `test_migration.py` — `alembic upgrade head` → 7 columns + backfill all existing rows; `downgrade -1` clean.
  - `test_apify_provider.py::test_apify_token_never_appears_in_logs` — log capture로 토큰 노출 확인 (REQ-ONBOARD-SEC-001).
  - `test_pinterest_url_validation.py::test_20_attack_urls_classify_none` — XSS/SSRF/IDN attack matrix.
- **Files**:
  - CREATE: `tests/test_onboarding/test_migration.py`
  - EXTEND: `tests/test_onboarding/test_apify_provider.py`
  - EXTEND: `tests/test_onboarding/test_pinterest_url_validation.py`
- **REQ**: REQ-ONBOARD-MIGRATION-001, REQ-ONBOARD-MIGRATION-002, REQ-ONBOARD-SEC-001
- **Complexity**: M
- **Predecessors**: ONB-T01, ONB-T03, ONB-T04
- **Verification**: 전체 green + log capture가 토큰/raw URL 0건 매치.

### ONB-T22 — LOG-T23 xfail-strict cleanup + coverage gate + ruff

- **Description**: Cross-SPEC cleanup — SPEC-CONVERSATION-LOG-001 `tests/test_conversation_log/test_payload_shapes.py::test_taste_update_unimplemented_source_xfail` 의 `@pytest.mark.xfail(strict=True)` 마커 제거 (xpass 상태가 되어 strict mode가 fail). `_UNIMPLEMENTED_SOURCES` set 에서 `"onboard"`, `"pinterest"` 두 항목 `_IMPLEMENTED_SOURCES` 로 이동. AST scan 으로 `taste_update.source="onboard"` AND `="pinterest"` emit site 검증. Coverage gate `pytest --cov` ≥ 85% per new module (plan §11.3 list). `ruff check . && ruff format --check .` pass. `pytest -q` overall count ≥ pre-SPEC baseline.
- **Files**:
  - MODIFY: `tests/test_conversation_log/test_payload_shapes.py`
  - REVIEW: 전체
  - UPDATE: SPEC `acceptance.md` (별도 — DoD 결과 매핑)
- **REQ**: SPEC-CONVERSATION-LOG-001 LOG-T23 cascade, REQ-ONBOARD-OBS-001 cascade (catalog activation)
- **Complexity**: S (cleanup) + M (acceptance.md authoring)
- **Predecessors**: ONB-T13 ~ ONB-T16 (모든 emit sites 존재)
- **Verification**: `pytest tests/test_conversation_log/test_payload_shapes.py -v` green (xfail markers 사라지고 implemented set에서 정상 pass). `pytest -q` overall count check.

---

## Dependency Graph (summary)

```
ONB-T01 (migration 0004)────────────────────┐
ONB-T02 (option catalog)────────┐           │
ONB-T03 (classifier)─────┐      │           │
ONB-T04 (apify provider) │      │           │
ONB-T05 (seed_from_onboarding)  │           │
ONB-T06 (resolve_batch)──┤      │           │
ONB-T07 (config env vars)│      │           │
                         ▼      ▼           │
              ONB-T08 (card builders)       │
              ONB-T09 (_pinterest_helpers)──┤
              ONB-T10 (_complete_onboarding)│
                         │                  │
                         ▼                  │
              ONB-T11 (state + Session ext) ◀
                         │
                         ▼
              ONB-T12 (onboard_intro)
              ONB-T13 (mood/color/fit)
              ONB-T14 (fit → END branch — in T13)
              ONB-T15 (onboard_pinterest)
              ONB-T16 (pinterest_ingest)
                         │
                         ▼
              ONB-T17 (graph topology)
              ONB-T18 (routing functions)
              ONB-T19 (webhook /start + lifespan)
                         │
                         ▼
              ONB-T20 (13 manual scenarios automated)
              ONB-T21 (migration + privacy + GIN tests)
              ONB-T22 (LOG-T23 xfail cleanup + coverage gate)
```

---

## Parallelization Opportunities

- **Phase 1**: ONB-T01, T02, T03, T04, T06, T07 all parallel (서로 다른 파일). T05 도 parallel가능 (taste_profile.py / _pg.py).
- **Phase 2**: ONB-T08 (T02 의존), T09 (T03+T04+T05+T06 의존), T10 (T05+T09 의존) — T08 / T09 / T10 sequential.
- **Phase 3**: ONB-T11 (single-thread, foundation). T12/T13/T15/T16 모두 T11 land 후 parallel — 서로 다른 노드 파일.
- **Phase 4**: ONB-T17/T18 sequential (topology 가 routing 전제), T19 parallel with T20/T21/T22. T22는 final.

권장 PR 분할 (plan §10.1):
- PR-001: ONB-T01 + T02 + T03 + T04 + T06 + T07 (foundation, ~2 days)
- PR-002: ONB-T05 + Protocol amendment verification (~1 day)
- PR-003: ONB-T08 + T09 + T10 (helpers, ~2 days)
- PR-004: ONB-T11 + T12 + T13 + T14 (state + 4 onboarding nodes, ~3 days)
- PR-005: ONB-T15 + T16 (Pinterest stage + continuous, ~2 days)
- PR-006: ONB-T17 + T18 + T19 (graph wiring, ~1 day)
- PR-007: ONB-T20 + T21 + T22 (validation + cleanup, ~2 days)

---

## Per-Task Acceptance Tracking

각 ONB-T## 가 DoD 의 어느 checkbox에 매핑되는지 `acceptance.md`에서 한 번 더 정리됨 (ONB-T22 deliverable). tasks.md는 ordering + 의존성 + complexity 책임.

End of tasks.md.
