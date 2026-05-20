---
id: SPEC-AGENT-UX-P0-001
acceptance_version: 0.2.0
spec_version: 0.2.0
plan_version: 0.2.0
created: 2026-05-20
status: planned
---

# Acceptance Mapping — SPEC-AGENT-UX-P0-001

> SPEC §Definition of Done 의 P0 항목 + 6 manual scenarios (a-f) 를 실제 테스트 파일과 운영 검증 절차에 매핑한다.
>
> **Acceptance HISTORY**:
> - 2026-05-20 (v0.2.0): REQ-UX-004 매핑 추가 — 3 new test files (`test_pre_messages.py`, `test_pre_messages_nodes.py`, `test_pre_messages_tools.py`) + manual scenarios (d)/(e)/(f).
> - 2026-05-20 (v0.1.0): 초안 — 4 REQ-row.
>
> **Test layout**: 7 new files under `tests/test_diversify/` / `tests/test_agents/` / `tests/test_channels/` / `tests/test_graphs/`.
> 모든 테스트는 unit 레벨 (testcontainers Postgres 불필요) — CI 즉시 실행 가능.

---

## 1. P0 Requirements → Test Files

| REQ | DoD bullet | Test file | Test case(s) | Status |
|---|---|---|---|---|
| REQ-UX-001 | `diversify_service` adds `seen_ids` guard + `drops_dup` counter; falsy id bypass; byte-identical on unique-id input | `tests/test_diversify/test_diversify_dedup.py` | `test_dedup_drops_duplicate_id`, `test_missing_id_bypass_dedup`, `test_byte_identical_on_unique_ids`, `test_drops_dup_in_log_line` | planned |
| REQ-UX-002 | `run_react_loop` appends `[LANG=<ko|en> — MUST reply in <Korean|English>]` as LAST line of `system_content`; KO/EN snapshot; sticky across button-tap | `tests/test_agents/test_react_loop_lang_directive.py` | `test_system_prompt_ko_directive_last_line`, `test_system_prompt_en_directive_last_line`, `test_system_prompt_persona_and_memory_unchanged`, `test_sticky_lang_across_button_tap_turn` | planned |
| REQ-UX-003 (adapter) | `MessengerAdapter` ABC default returns False; `TelegramAdapter.send_chat_action` POSTs sendChatAction with fail-open swallow | `tests/test_channels/test_telegram_chat_action.py` | `test_abc_default_returns_false`, `test_send_chat_action_posts_to_telegram`, `test_send_chat_action_fail_open_on_http_500`, `test_send_chat_action_fail_open_on_timeout` | planned |
| REQ-UX-003 (hook) | ReAct loop fires `send_chat_action` exactly once before `search_products` / `refine_search` / `respond`; no leakage to other tools | `tests/test_agents/test_react_loop_typing_hook.py` | `test_typing_fired_before_search_products`, `test_typing_fired_before_refine_search`, `test_typing_fired_before_respond`, `test_typing_not_fired_for_other_tools` | planned |
| REQ-UX-004 (module) | `PRE_MESSAGES` dict 4 keys × KO/EN non-empty; `fire_pre_message` helper sends correct text + marks idempotency + fail-open swallow; import-site AST scan limits to 5 runtime sites | `tests/test_channels/test_pre_messages.py` | `test_pre_messages_shape_snapshot`, `test_fire_pre_message_sends_correct_text` (parametric 4 keys × KO/EN), `test_fire_pre_message_idempotent`, `test_fire_pre_message_fail_open_swallow`, `test_pre_messages_import_sites_ast_scan` | planned |
| REQ-UX-004 (graph nodes) | `vision_node` and `pinterest_ingest` fire pre-message exactly once at entry (before underlying LiteLLM/Apify call) with `PRE_MESSAGES[<vision|pinterest>][lang]`; idempotent on state marker; fail-open does not block underlying op | `tests/test_graphs/test_pre_messages_nodes.py` | `test_vision_node_fires_pre_message_ko`, `test_vision_node_fires_pre_message_en`, `test_vision_node_idempotent_on_reentry`, `test_pinterest_ingest_fires_pre_message_ko`, `test_pinterest_ingest_fires_pre_message_en`, `test_pinterest_node_send_text_failure_does_not_block_apify` | planned |
| REQ-UX-004 (tools) | `search_products` / `refine_search` / `analyze_image` dispatch each fires pre-message exactly once at entry; search and refine share `PRE_MESSAGES["search"]`; idempotent on ctx marker; pre-message fires BEFORE typing indicator; fail-open does not block tool body | `tests/test_agents/test_pre_messages_tools.py` | `test_search_products_dispatch_fires_search_message_ko`, `test_refine_search_dispatch_fires_same_search_message`, `test_analyze_image_dispatch_fires_analyze_image_message_en`, `test_tools_idempotent_within_same_ctx`, `test_ordering_pre_message_before_typing_indicator`, `test_search_dispatch_send_text_failure_does_not_block_search`, `test_analyze_image_dispatch_send_text_failure_does_not_block_vision`, `test_each_dispatch_uses_session_lang` (parametric 3 dispatch × KO/EN) | planned |

---

## 2. Definition of Done Manual Scenarios (a-f)

SPEC §Definition of Done 의 6 시나리오 — 자동화 가능한 부분은 위 표에 포함됨. 나머지는 dev Telegram bot 실측 procedure. (a-c) 는 v0.1.0, (d-f) 는 v0.2.0 REQ-UX-004 신규.

### (a) Duplicate-card regression — 0 duplicate `product_id` in carousel

- **Automated coverage**: `test_diversify_dedup.py::test_dedup_drops_duplicate_id` (synthetic dup-id fixture).
- **Manual verification** (dev bot, `@kiko_fashion_ai_bot` DEV token):
  ```bash
  # Telegram → DM bot → send a query that historically produced duplicates
  # (또는 dev-ai 로그에서 drops_dup=0 이 아닌 turn 의 후속 카드를 캡처)
  ssh -i ~/Desktop/aws-infra/kikoai-key.pem ec2-user@54.116.116.225 \
    'docker logs ai-server --tail 500 2>&1 | grep "drops_dup"'
  # 기대: drops_dup=N>=1 인 turn 이 나오면, 그 turn 의 user 화면에 동일 product 카드 0개여야 함.
  ```

### (b) Sticky language — Korean conversation stays Korean after button-tap

- **Automated coverage**: `test_react_loop_lang_directive.py::test_sticky_lang_across_button_tap_turn` (fake LLM with session fixture).
- **Manual verification**:
  ```
  # Telegram session:
  1) 사용자: "안녕"           → bot: 한국어 응답
  2) 사용자: 카드 인라인 버튼 탭 (텍스트 없음)
  3) 봇 응답이 한국어인지 확인 (영어 드리프트 없음)
  ```
  Langfuse trace (활성화된 경우): 해당 turn 의 system message 끝 라인이 `[LANG=ko — MUST reply in Korean]` 인지 확인.

### (c) Typing indicator visible during search/LLM phase

- **Automated coverage**: `test_telegram_chat_action.py::test_send_chat_action_posts_to_telegram` (httpx mock) + `test_react_loop_typing_hook.py::test_typing_fired_before_search_products` (spy adapter).
- **Manual verification**:
  ```
  # Telegram session:
  1) 사용자: 패션 사진 1장 전송
  2) Telegram 채팅창 상단에 "kiko 입력 중…" 인디케이터 등장 (~5s)
  3) 추천 카드 도착
  ```
  Long-running turns: 두 번째 tool dispatch (refine) 시 indicator 재등장 가능.

### (d) Vision pre-message visible before analysis (REQ-UX-004)

- **Automated coverage**: `test_pre_messages_nodes.py::test_vision_node_fires_pre_message_ko` + `test_pre_messages_tools.py::test_analyze_image_dispatch_fires_analyze_image_message_en`.
- **Manual verification**:
  ```
  # Telegram session (lang=ko):
  1) 사용자: 패션 사진 1장 전송
  2) 봇 메시지 즉시 등장: "사진 잘 봤어요, 잠깐 분석해볼게요 👀"
  3) "kiko 입력 중…" 인디케이터 (REQ-UX-003) 등장
  4) Vision 결과 → 후속 검색/응답
  ```
  EN 케이스: `/start` 후 영어로 시작 (`Hi`) → 사진 → 봇 메시지: "Got it! Let me take a closer look 👀".

### (e) Search pre-message visible before carousel (REQ-UX-004)

- **Automated coverage**: `test_pre_messages_tools.py::test_search_products_dispatch_fires_search_message_ko` + `test_refine_search_dispatch_fires_same_search_message`.
- **Manual verification**:
  ```
  # Telegram session (lang=ko):
  1) 사용자: "청바지 추천"
  2) 봇 메시지 즉시: "잠시만요, 마음에 들 만한 거 찾아볼게요 🔍"
  3) typing indicator 등장
  4) 카드 카르셀 도착
  5) "다르게 찾기" 버튼 탭 → 같은 메시지 다시 등장 (다른 turn = 새 marker)
  ```

### (f) Pinterest pre-message visible before scrape (REQ-UX-004)

- **Automated coverage**: `test_pre_messages_nodes.py::test_pinterest_ingest_fires_pre_message_ko`.
- **Manual verification** (dev only, `PINTEREST_BOOTSTRAP_ENABLED=true` + `APIFY_TOKEN` 필요):
  ```
  # Telegram session — 온보딩 Pinterest stage:
  1) 사용자: Pinterest board URL 전송
  2) 봇 메시지 즉시: "보드 살펴볼게요, 잠시만요 📌"
  3) Apify 스크래핑 (수초~수십초)
  4) TasteProfile reinforce 결과
  ```
  PINTEREST_BOOTSTRAP_ENABLED 가 꺼져 있다면 본 시나리오 skip (자동화 coverage 만으로 acceptance).

---

## 3. AST / Static Checks

| Check | Tool | File | Test case |
|---|---|---|---|
| `send_chat_action` 호출 site 가 3개 tool name 분기에 한정 | `ast.parse` | `app/agents/react_loop.py` | `test_react_loop_typing_hook.py::test_typing_not_fired_for_other_tools` |
| `TelegramAdapter.send_chat_action` 내부에서 새 `httpx.AsyncClient(` instantiation 금지 (기존 client 재사용 보장) | `ast.parse` | `app/channels/telegram/adapter.py` | `test_telegram_chat_action.py` (별도 case 또는 conftest scan) |
| `LANG_NAME` dict 의 모든 키가 `app/channels/lang.py::detect_lang` 의 반환 집합과 일치 | parametric | `app/agents/react_loop.py` + `app/channels/lang.py` | `test_react_loop_lang_directive.py` (parametric over `["ko", "en"]`) |
| `PRE_MESSAGES` / `fire_pre_message` import 가 5개 site (`vision.py`, `search_products.py`, `pinterest_ingest.py`, `analyze_image.py`) + 자기 자신 + 테스트 파일에만 존재 | path glob + source scan | `app/**/*.py` | `test_pre_messages.py::test_pre_messages_import_sites_ast_scan` |
| `PRE_MESSAGES` 키 셋트 = `{vision, search, pinterest, analyze_image}` 정확히 | dict assertion | `app/channels/pre_messages.py` | `test_pre_messages.py::test_pre_messages_shape_snapshot` |

---

## 4. Final Gates (target)

| Gate | Command | Target Result |
|---|---|---|
| ruff check | `uv run ruff check .` | PASS |
| ruff format | `uv run ruff format --check .` | PASS |
| pytest new | `uv run pytest tests/test_diversify/test_diversify_dedup.py tests/test_agents/test_react_loop_lang_directive.py tests/test_agents/test_react_loop_typing_hook.py tests/test_channels/test_telegram_chat_action.py tests/test_channels/test_pre_messages.py tests/test_graphs/test_pre_messages_nodes.py tests/test_agents/test_pre_messages_tools.py -q` | All planned cases pass |
| pytest overall | `uv run pytest -q` | No regression vs pre-SPEC baseline |
| Manual scenarios (a)-(f) | dev bot smoke | All 6 pass observation (f is conditional on `PINTEREST_BOOTSTRAP_ENABLED`) |

---

## 5. Plan Deviations (filled at completion)

_To be populated at SPEC completion. Expected examples:_

- (anticipated) Helper extraction: if `react_loop.py` 의 system_content 조립이 `run_react_loop` 본문 안 inline 이면, snapshot test 를 위해 작은 helper 로 extract 했는지 / 또는 LLM mock 으로 캡처했는지 — 결정 기록.
- (anticipated) AST test 의 정확한 패턴 (ast walk vs source regex) — plan §2.4 의 결정 기록.
- (anticipated, REQ-UX-004) `refine_search` 가 별도 wrapper 파일인지 `search_products.py` 내 분기인지 확정 결과. AST allow-list 가 그에 맞춰 조정되었는지.
- (anticipated, REQ-UX-004) Graph node 의 `state.__dict__` 직접 mutation 이 LangGraph 의 state 불변성 제약과 충돌하지 않았는지 — `state.dict_marker` 같은 별도 필드로 분리되었는지.
- (anticipated, REQ-UX-004) `adapter.send_text` 의 실제 메서드명/시그니처 (A9 검증 결과) — `send_text(chat_id, text)` 가 아니라 다른 변종이었다면 helper 시그니처 변경 기록.

---

## 6. Status: PLANNED

SPEC-AGENT-UX-P0-001 v0.2.0 ready for Run phase. 7 new test files planned (16 v0.1.0 cases + ~22 REQ-UX-004 cases ≈ 38 new test cases). No new env vars, no migrations, no external service dependencies — pure code/test change confined to 4 modified source files + 1 NEW module (`app/channels/pre_messages.py`) + 5 firing-site edits (`vision.py`, `search_products.py`, `pinterest_ingest.py`, `analyze_image.py`).
