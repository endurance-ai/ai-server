---
id: SPEC-OBSERVABILITY-002
version: 0.2.0
status: draft
created_at: 2026-05-11
updated: 2026-05-11
author: hchsa77@gmail.com
priority: P0
issue_number: null
labels: [observability, langfuse, langgraph, telemetry]
---

# SPEC-OBSERVABILITY-002: Langfuse v3 Activation for LangGraph Telegram Fashion Bot

## HISTORY

- 2026-05-11 (v0.2.0): plan-auditor 1차 감사(0.72) 반영. D1/D2 (frontmatter: `created`→`created_at`,
  `labels` 추가), D3 (REQ Index 추가), D6 (context7 + langfuse-python v3.4.0 소스 직접 확인으로
  v3 import 경로 확정: `from langfuse import observe` top-level / `from langfuse.langchain import
  CallbackHandler` — 메인 `langfuse` 패키지 번들, `langfuse-langchain` 별도 dist 아님), D7 (R4
  fallback 강화: `selective_mode` REQ-OBS-COST-002 신설 — p99 > 5ms 시 LLM 노드 4개만 데코레이트,
  rollback 절차 명시. REQ-OBS-TRACE-NODE-001 에 partial-decoration fallback 단서 추가), D10
  (REQ-OBS-FALLBACK-002 신설 — mid-session host 실패 시나리오), D4 (Requirements 헤더 →
  "Requirements & Acceptance Criteria"), D5 (REQ-OBS-COST-001 baseline 명시 — keys-unset cascade
  collapse), D9 (REQ-OBS-TRACE-LOOP-001 metadata 메커니즘 확정: `langfuse.update_current_span(
  metadata={...})` v3 client API), D11 (langchain 통합은 main `langfuse` 패키지 번들 — 추가
  dependency 불필요. pyproject 변경 1줄로 축소), D12 (Non-Goals #13 중복 항목 제거), D14 (DoD
  manual E2E 시나리오에 REQ-ID annotation 부착). OQ-1 resolved. 총 14개 결함 반영.
- 2026-05-11 (v0.1.0): 초안 작성. SPEC-AGENT-001 REQ-OBSV-002 acceptance #4 가
  명시적으로 허용한 "no-op fallback" 상태를 종료한다. 현재 `app/observability/langfuse.py::build_callback_handler` 는
  langfuse v2 의 `CallbackHandler` 가 `from langchain.callbacks.base import BaseCallbackHandler` 를
  요구하는데 langchain >= 1.0 (langgraph 1.x 가 langchain-core 1.3+ 를 통해 끌고 오는 버전)
  에서 해당 모듈이 제거되어 항상 `None` 을 반환한다. 결과적으로 `respond` / `ask_clarify` /
  `evaluator` / `apply_clarify` 노드의 nested LLM 호출이 Langfuse trace tree 에 안 잡힌다.
  본 SPEC 은 의존성을 `langfuse>=3,<4` 로 올리고, v3 의 `langfuse.langchain.CallbackHandler` 와
  `langfuse.observe` 를 사용하도록 `app/observability/langfuse.py` 를 재작성한다.
  SPEC-MEMORY-001 (Postgres backend, 방금 머지) 이 추가한 `@observe(name="memory.session.update")` /
  `memory.taste.*` 스팬들은 본 SPEC 활성화 즉시 trace tree 에 나타난다 — 추가 코드 변경 없음.
  Langfuse self-host 인프라는 이미 dev-app EC2 (`aws-infra/kiko-ai-servers/portal-ai/`) 에
  배포되어 있고 `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` 환경변수도
  설정 완료 — 본 SPEC 은 서버 측 변경 없음.

---

## Goal

현재 텔레그램 봇의 관측 상태는 다음과 같다:

- `app/observability/langfuse.py::observe(...)` 는 langfuse v2/v3 import 를 시도하지만 환경에
  설치된 패키지가 v2 라서 `langfuse.decorators.observe` 만 잡힌다. 이 자체는 동작하므로
  `app/pipeline/runner.py::run_pipeline` 및 SPEC-MEMORY-001 이 새로 데코레이트한 4개 메서드
  (`memory.session.update` / `get_or_create`, `memory.taste.update` / `get_or_create`) 의 spans 는
  현재 *Langfuse 가 활성이라면* trace 로 잡히긴 한다. 하지만 —
- `build_callback_handler(...)` 는 항상 `None` 을 반환한다 (langfuse v2 `CallbackHandler` 가
  langchain<1.0 의 `BaseCallbackHandler` 를 요구 → langchain 1.x 에 그 경로가 없음). 결과적으로
  LangGraph 노드 내부의 `ChatOpenAI` 호출 (`respond`, `ask_clarify`, `critique_apply`, `evaluator`)
  은 Langfuse 의 nested generation span 으로 잡히지 않는다.
- 이 두 사실의 합집합 — pipeline 레벨 span 은 보이는데 그 안의 LLM call 은 트리에 없는 — 이
  현재 trace 의 비대칭이다. SPEC-AGENT-001 REQ-OBSV-002 acceptance #4 가 "no-op fallback OK" 라고
  명시했지만 그건 어디까지나 임시 상태였다.

본 SPEC 은 이 비대칭을 종료한다:

1. `langfuse` 의존성을 v2 에서 **v3 (>=3,<4)** 로 올린다. v3 의 `langfuse.langchain.CallbackHandler`
   는 langchain >= 1.0 의 callbacks 인터페이스 (`langchain_core.callbacks.BaseCallbackHandler`) 와
   호환되어, langgraph 1.x 와 함께 깨끗하게 동작한다.
2. `app/observability/langfuse.py::build_callback_handler(...)` 가 **실제로 working v3 handler 를
   반환** 하도록 재작성한다. 키 없음 / host 도달 불가 시 `None` 반환 + WARN 로그 (graceful
   degradation) 는 그대로 유지.
3. `observe(...)` 데코레이터를 **v3 API 단일 경로** (`from langfuse import observe`) 로 통일한다.
   현재의 v3 → v2 → no-op cascade 는 v3 → no-op 2단으로 단순화. v2 fallback 분기는 byte-clean
   제거 (orphan import 0, ruff clean).
4. 12 노드 모두에 entry span 이 생기도록 한다. Reflexion 루프 (`search → evaluator → critique_apply →
   search`) 가 단일 parent trace 아래 retry-indexed children 으로 보이도록 한다. Clarify cards
   emission (`ask_clarify`) 과 callback consumption (`apply_clarify`) 이 두 개의 분리된 span 으로
   같은 `session_id` 아래 묶이도록 한다.
5. **PII 제거 강제**. SPEC-AGENT-001 REQ-OBSV-005 가 정의한 sha256-prefix-16 해시 규칙을 단일
   helper (`_hash_for_span`) 로 통합하고, raw `chat_id` / `from_user_id` 가 span attribute 어디에도
   나타나지 않음을 단위 테스트로 강제한다.
6. Trace metadata 표준화: `session_id`, `user_id`, `lang`, `flow` (image/text/callback), `critique_retry_count`,
   `chat_id_hash` (raw 아님) 가 모든 webhook trace 의 root span 에 부착되도록 한다.

핵심 설계 원칙:

- **Server-side 무변경**. Langfuse self-host 는 dev-app 에 이미 떠 있고 이번 SPEC 은 서버 자체 변경
  / 새 프로젝트 / 새 env var 도입을 하지 않는다. 클라이언트 SDK 만 v3 로 올린다.
- **Graceful degradation 유지**. `LANGFUSE_*` 키가 비어 있거나 host 가 도달 불가하면 데코레이터는
  no-op 으로 동작하고 봇은 정상 부팅. SPEC-MEMORY-001 의 `MEMORY_FALLBACK_ON_PROBE_FAIL` 과 동일한
  pattern.
- **외부 행위 byte-identical**. 사용자가 보는 메시지, 추천 결과, KO/EN sticky 언어, clarify 카드
  흐름, 검색 결과 — 모두 변화 없음. Tracing 은 *side effect* 이다.
- **v2 잔재 0**. 의존성 트리, import 경로, 주석에서 langfuse v2 흔적을 모두 제거. 미래 개발자가
  "왜 v2 / v3 cascade 가 있지?" 라고 묻지 않도록.
- **SPEC-MEMORY-001 의존성 명시화**. memory 모듈은 이미 `@observe(name=...)` 데코레이트를 끝냈다.
  본 SPEC 활성화는 추가 코드 없이 memory span 을 trace tree 에 띄운다.

이 SPEC 은 **WHAT** 과 **WHY** 만 정의한다. 정확한 import 경로 변경 시 발생할 수 있는 v3
콜백 핸들러의 메서드 시그니처 미세 차이, span name 표기 컨벤션, retry-iteration 의 정확한 nesting
방식 (parent span 1개 + child N개 vs sequential N+1개) 등은 `plan.md` 와 Run phase 에서 결정한다.

---

## Background

### 현재 상태 (langfuse v2 + langchain 1.x = nested LLM 미관측)

`app/observability/langfuse.py` 의 두 entry point 가 처리하는 두 종류의 spans:

1. **`observe(...)` 데코레이터** — `app/pipeline/runner.py::run_pipeline`,
   `app/channels/session_pg.py::PostgresSessionStore.{update,get_or_create}` (SPEC-MEMORY-001),
   `app/channels/taste_profile_pg.py::PostgresTasteProfileStore.{update,get_or_create}` (SPEC-MEMORY-001)
   네 메서드 + 파이프라인 1개에 부착되어 있다. v2 의 `langfuse.decorators.observe` 가 import 되어
   현재도 동작은 한다 (Langfuse 가 활성 키를 받은 경우).

2. **`build_callback_handler(...)` 팩토리** — LangGraph `graph.ainvoke(..., config={"callbacks":
   [handler]})` 에 넣을 langchain-호환 callback handler 를 만들어야 한다. v2 의 `langfuse.callback.
   CallbackHandler` 가 module top-level 에서 `from langchain.callbacks.base import BaseCallbackHandler`
   를 import 하고, 그 모듈은 langchain >= 1.0 에서 제거됐다. 따라서 `build_callback_handler` 는
   **항상 `None` 을 반환** 한다. SPEC-AGENT-001 REQ-OBSV-002 acceptance #4 가 이를 명시적으로 허용
   ("no-op fallback 가능").

결과적으로 trace tree 의 모양은:

```
[trace root: webhook handler]
  ├── (span) pipeline.run            ← @observe 로 잡힘 ✓
  │     └── (nested LLM calls 안 잡힘 — callbacks=[] 빈 채로 langchain runnable 호출)
  ├── (span) memory.session.update   ← SPEC-MEMORY-001, @observe 로 잡힘 ✓
  │
  └── (LangGraph 노드 들 — span 없음)
        └── respond → ChatOpenAI.ainvoke(...) — 트리에 흔적 없음 ✗
        └── ask_clarify → ChatOpenAI.ainvoke(...) ✗
        └── critique_apply → ChatOpenAI.ainvoke(...) ✗
        └── evaluator → LLMProvider.chat(...) — 별도 @observe 없음 ✗
```

이 비대칭은 디버깅 시 가장 비싼 부분 (LLM latency, token usage, prompt content) 을 가려버린다.
또한 SPEC-AGENTIC-CRITIQUE-001 의 Reflexion 루프 (`search → evaluator → critique_apply → search`)
는 retry index 가 trace 에 보이지 않으므로 "왜 retry 가 3번 돌았는가" 같은 질문에 즉답할 수 없다.

### Langfuse v3 변경점 (요약)

context7 MCP + langfuse-python v3.4.0 GitHub 소스 (`langfuse/__init__.py`,
`langfuse/langchain/__init__.py`, `langfuse/_client/client.py`) 를 통해 다음 사실을 SPEC 작성 시점
(2026-05-11) 에 직접 확인 완료:

- **`from langfuse import observe`** — top-level 노출 (`langfuse/__init__.py` 의 `__all__` 에 포함).
  v2 의 `langfuse.decorators.observe` 는 v3 에서 제거됐다 (deprecated alias 아님 — 그냥 없음).
- **`from langfuse.langchain import CallbackHandler`** — v3 의 정식 경로. **메인 `langfuse` 패키지
  안에 sub-module 로 번들** 되어 있다. **별도 `langfuse-langchain` distribution 은 존재하지 않음**
  (PyPI 에 그 이름의 패키지 없음). 내부 구현은 `langfuse/langchain/CallbackHandler.py` 의
  `LangchainCallbackHandler` 를 `CallbackHandler` 라는 이름으로 re-export. langchain >= 1.0 의
  `langchain_core.callbacks.BaseCallbackHandler` 와 호환.
- **`langfuse.update_current_span(metadata={...})` / `langfuse.update_current_trace(...)`** — v3
  client 인스턴스의 메서드. 함수 본문 안에서 동적으로 metadata 를 attach 할 때 사용
  (`langfuse/_client/client.py` 에서 확인). v2 의 `langfuse_context.update_current_observation` 은
  v3 에서 제거됐다. `get_client().flush()` 가 lifespan exit 의 flush API.
- **API key/host 환경변수** (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`) 는 v2 와
  동일. 본 SPEC 은 env 추가 0.
- **Server-side wire protocol** 은 v2 ↔ v3 client 모두 호환되도록 후방 호환 유지. self-host 인스턴스
  업그레이드 없이 v3 client 가 데이터 전송 가능 (Langfuse 공식 문서 + `plan.md` 의 실측 spike 로 재확인).

위 사실들은 `plan.md` 의 추가 실측 없이 즉시 REQ AC 에 반영됨 (OQ-1 / OQ-2 해소).

### Telegram bot context (왜 nested tracing 이 중요한가)

SPEC-AGENT-001 의 12-node StateGraph 는 다음 LLM 호출 지점을 갖는다:

| Node | LLM call | Library |
|---|---|---|
| `respond` | 자연어 reply 생성 | `langchain-openai.ChatOpenAI` |
| `ask_clarify` | clarify 카드는 결정론적 (SPEC-CLARIFY-CARDS-001) — LLM 호출 없음 | — |
| `critique_apply` | 자유 텍스트 critique 분류 | LiteLLM via httpx |
| `evaluator` | search 결과 평가 (SPEC-AGENTIC-CRITIQUE-001) | LiteLLM via httpx |
| `vision` | Vision 패션 아이템 추출 | LiteLLM via httpx |
| `pick_item` | (텍스트 라벨링만) — 직접 LLM 호출 없음 | — |

`langchain-openai.ChatOpenAI` 를 쓰는 노드 (`respond`) 는 **callback handler 가 working 일 때만**
LangChain 의 callback 인프라를 통해 자동으로 Langfuse span 을 만든다. LiteLLM via httpx 직접 호출인
노드 (`critique_apply`, `evaluator`, `vision`) 는 모듈에 `@observe` 가 부착되어 있으면 (또는
부착할 수 있으면) span 을 만든다.

본 SPEC 의 목표는 두 경로 모두 trace tree 에 묶이게 하는 것이다.

### SPEC-MEMORY-001 과의 상호작용

SPEC-MEMORY-001 (방금 머지) 은 4개의 새 메서드에 `@observe(name="memory.{session,taste}.{update,get_or_create}")`
를 부착했다. 본 SPEC 활성화 시 (= langfuse v3 working + 키 설정) 그 span 들은 별다른 코드 변경 없이
trace tree 에 자동으로 나타난다. 본 SPEC 은 memory 모듈을 건드리지 않는다.

### Cost

Langfuse self-host 가 dev-app EC2 에 이미 떠 있다. CPU/RAM/디스크 모두 현재 사용량 대비 충분한 여유.
v3 SDK 가 보내는 wire data 의 size 는 v2 와 본질적으로 동일 (span attribute payload 표준화). 따라서
서버 측 cost = 0. 클라이언트 측 latency overhead 는 < 5ms p99 (SDK 가 background queue 로 send 하고
hot path 에서는 enqueue 만) — REQ-OBS-COST-001 에서 측정 방법 정의.

---

## Architecture Snapshot (informative)

Today (langfuse v2 + langchain 1.x):

```
LangGraph webhook handler
   │
   ├── graph.ainvoke(state, config={"callbacks": [None ← always None]})
   │     │
   │     ├── nodes/respond.py
   │     │     └── ChatOpenAI.ainvoke(messages, config=config)
   │     │           └── callbacks=[None] → Langfuse trace NOT recorded ✗
   │     │
   │     ├── nodes/evaluator.py
   │     │     └── LLMProvider.chat(...) — no @observe wrapper ✗
   │     │
   │     └── nodes/critique_apply.py
   │           └── LLMProvider.chat(...) ✗
   │
   └── pipeline.run_pipeline(state)
         │
         └── @observe(...) ✓  (v2 decorators path — works)
               └── (nested calls inside still not parented because no callback handler)
```

After this SPEC (langfuse v3 + langchain 1.x):

```
LangGraph webhook handler
   │
   ├── observe(name="webhook.telegram", session_id=hash, user_id=hash, metadata={...})
   │   wraps the handler — opens ROOT trace
   │     │
   │     ├── handler = build_callback_handler(session_id=..., user_id=..., metadata=...)
   │     │     ↓ returns langfuse.langchain.CallbackHandler  ✓ (v3)
   │     │
   │     ├── graph.ainvoke(state, config={"callbacks": [handler]})
   │     │     │
   │     │     ├── @observe(name="node.ingest")   ✓
   │     │     ├── @observe(name="node.vision")   ✓ → nested generation span (LiteLLM call attrs)
   │     │     ├── @observe(name="node.pick_item")  ✓
   │     │     ├── @observe(name="node.ask_clarify") ✓ (no LLM child)
   │     │     ├── @observe(name="node.apply_clarify") ✓
   │     │     ├── @observe(name="node.critique_apply") ✓ → generation span
   │     │     ├── @observe(name="node.search") ✓
   │     │     ├── @observe(name="node.evaluator") ✓ → generation span
   │     │     │     (retry index attribute on the span)
   │     │     ├── @observe(name="node.send_results") ✓
   │     │     ├── @observe(name="node.taste_update") ✓
   │     │     │     └── memory.taste.{update,get_or_create} (SPEC-MEMORY-001) ✓
   │     │     ├── @observe(name="node.respond") ✓
   │     │     │     └── ChatOpenAI.ainvoke(messages, config={"callbacks":[handler]})
   │     │     │           └── handler captures: model, prompt_tokens, completion_tokens, latency ✓
   │     │     └── @observe(name="node.resolve_image") ✓
   │     │
   │     └── pipeline.run_pipeline(state) — @observe ✓ (unchanged from today)
   │           └── memory.session.{update,get_or_create} ✓ (inherits trace via SPEC-MEMORY-001 deco)
   │
   └── (on shutdown: get_client().flush() in lifespan to drain queue)

Fallback path (LANGFUSE_PUBLIC_KEY absent OR LANGFUSE_HOST unreachable at startup):
    observe(...) → no-op decorator (identical to today's no-op branch)
    build_callback_handler(...) → None (config={"callbacks": []})
    Bot starts cleanly. WARN log emitted. ✓
```

**Affected modules in kikoai/ai (this SPEC)**:

- `app/observability/langfuse.py` — REWRITE. v3 단일 경로, v2 cascade 제거, `build_callback_handler`
  가 working v3 handler 반환. `_hash_for_span` helper 추가 (sha256 prefix-16).
- `app/main.py` — MODIFIED. lifespan exit 에서 `get_client().flush()` 호출 추가 (background queue drain).
- `app/api/webhooks/telegram.py` — MODIFIED. webhook entry 에 `observe(name="webhook.telegram", ...)`
  적용; `build_callback_handler` 결과를 `graph.ainvoke` 의 `config.callbacks` 로 전달.
- `app/graphs/fashion_bot.py` — MODIFIED. `build_callback_handler` import 경로는 그대로지만, callback
  handler 가 `None` 이 아닌 working 객체일 때 LangGraph 가 모든 노드 invocation 의 callbacks 로
  propagate 하는지 verify (langgraph 1.x 의 RunnableConfig propagation 동작 확인). 코드 변경은
  최소 — propagation 은 LangGraph 가 자동.
- `app/graphs/nodes/ingest.py`, `resolve_image.py`, `vision.py`, `pick_item.py`, `ask_clarify.py`,
  `apply_clarify.py`, `critique_apply.py`, `search.py`, `evaluator.py`, `send_results.py`,
  `taste_update.py`, `respond.py` — MODIFIED (12 노드 모두). 각 노드 entry 함수에 `@observe(name=
  "node.{name}", as_type="span")` 데코레이터 부착. 노드 본문 로직 변경 없음.
- `app/graphs/nodes/evaluator.py` — MODIFIED (추가). LiteLLM 호출 부분에 `@observe(as_type=
  "generation")` 별도 부착 — `critique_retry_count` attribute 가 span 에 attach 되도록.
  (`@observe` 가 함수 인자에서 attribute 를 추출하는 v3 API 활용)
- `app/channels/vision.py`, `app/graphs/nodes/critique_apply.py` — MODIFIED (추가). 동일 패턴으로
  `as_type="generation"` 부착. 본문 변경 없음.
- `app/core/config.py` — 변경 없음. 기존 `LANGFUSE_*` env vars 그대로.
- `pyproject.toml` — MODIFIED. `langfuse` 핀 `>=3,<4` 로 변경. **추가 패키지 없음** — `langfuse.
  langchain` 은 main `langfuse` 패키지에 sub-module 로 번들 (v3.4.0 소스 확인 완료).
- `uv.lock` — REGENERATED.
- `tests/test_observability/test_langfuse.py` — NEW. `build_callback_handler` 가 v3 handler 를
  반환하는지, 키 없을 때 `None` 인지, `_hash_for_span` 이 결정론적 sha256 prefix-16 인지 검증.
- `tests/test_observability/test_trace_shape.py` — NEW. Mock Langfuse client 로 12 노드 span 이
  모두 emit 되는지, parent–child 관계가 올바른지 검증.
- `tests/test_observability/test_pii.py` — NEW. raw `chat_id` / `from_user_id` 가 어떤 span
  attribute 에도 안 나타나는지 정적 + 동적 검증.
- `tests/test_observability/test_latency.py` — NEW. Langfuse disabled vs enabled p99 latency 측정,
  < 5ms overhead 확인.

**Reused, untouched modules**:

- `app/channels/session.py`, `app/channels/session_pg.py` (SPEC-MEMORY-001 의 `@observe` 데코레이터
  그대로 — v3 API 가 `observe` 를 top-level 노출하므로 import 경로만 우리쪽 `app/observability/langfuse.py`
  의 re-export 로 통일하면 caller 무변경).
- `app/channels/taste_profile.py`, `app/channels/taste_profile_pg.py` — 동일.
- `app/pipeline/runner.py` — `@observe` 그대로.
- `app/graphs/state.py`, `app/graphs/routing.py` — state 모델 / 라우팅 함수 변경 없음.
- `app/channels/factory.py`, `app/channels/adapter.py`, `app/channels/telegram/*` — messenger
  어댑터 무관.

---

## Trace Shape Reference (informative — formalized in REQ-OBS-TRACE-*)

### Root trace

`observe(name="webhook.telegram", ...)` 가 webhook entry 에서 열린다.

| Attribute | Value | Source |
|---|---|---|
| `session_id` | `sha256(chat_id)[:16]` | SPEC-AGENT-001 REQ-OBSV-005 |
| `user_id` | `sha256(from_user_id)[:16]` (when present) | 동일 |
| `metadata.lang` | `"ko"` or `"en"` | `Session.lang` (sticky, SPEC-AGENT-001 ingest 노드) |
| `metadata.flow` | `"image"` / `"text"` / `"callback"` | webhook 파싱 결과 — `plan.md` 가 정확한 분류 함수 정의 |
| `metadata.chat_id_hash` | `sha256(chat_id)[:16]` (duplicated for searchability) | — |
| `metadata.critique_retry_count` | 최종 `WorkingState.critique_retry_count` (0–N) | graph completion 시 부착 |

### Child spans (12 nodes)

각 노드의 entry 함수에 `@observe(name="node.{n}", as_type="span")`.

| Node | as_type | LLM provider | Extra metadata |
|---|---|---|---|
| `ingest` | span | — | `flow` (image/text/callback) |
| `resolve_image` | span | — | `had_url`, `resolved_ok` |
| `vision` | span (with nested generation) | LiteLLM | `model`, `schema_version` (v1/v2), `weak_vision` (bool) |
| `pick_item` | span | — | `items_count` |
| `ask_clarify` | span | — | `axis_chosen`, `button_count` (SPEC-CLARIFY-CARDS-001 REQ-CLARIFY-OBSV-001 가 정의한 4개 attribute) |
| `apply_clarify` | span | — | `axis`, `value`, `boost_keywords_added` (count) |
| `critique_apply` | span (with nested generation) | LiteLLM | `critique_source` (callback/text), `delta_summary` |
| `search` | span | — | `candidates_count`, `applied_filters` (dict shape — keys only, no values) |
| `evaluator` | span (with nested generation) | LiteLLM | `iteration`, `score`, `retry`, `source` (fast_path/llm). SPEC-AGENTIC-CRITIQUE-001 REQ-CRITIQUE-OBSV-001 의 metadata 와 동일 키로 정렬 |
| `send_results` | span | — | `sent_count`, `shown_product_ids_count` |
| `taste_update` | span | — | `reinforce_count` (memory child spans 가 자식으로 나타남 — SPEC-MEMORY-001) |
| `respond` | span (with nested generation) | langchain-openai ChatOpenAI | `model`, `fallback_used` (bool), `flow` (Reflexion / fresh / clarify_followup / …) |

### Reflexion 루프 spans (REQ-OBS-TRACE-LOOP-001)

Reflexion 한 turn 에서 `search` → `evaluator` → (retry 시) `search` → `evaluator` → … 가 반복된다.
이 SPEC 에서 요구하는 trace 표현은:

옵션 A — **시퀀셜**: 각 iteration 의 span 들이 root trace 의 children 으로 순차 등장. retry index 는
각 `evaluator` span 의 `iteration` attribute 로 구분.

옵션 B — **그룹화**: `loop.reflexion` 이라는 합성 parent span 을 열어 그 안에 N+1 개의
`search`/`evaluator` 쌍을 child 로 묶음.

본 SPEC 은 **옵션 A** 를 채택. 이유: LangGraph 1.x 의 `@observe` 데코레이터가 같은 함수 호출 N 번에
대해 같은 부모 (root trace) 아래 N 개의 sibling span 을 만들도록 동작하는 것이 자연스러움.
옵션 B 는 합성 span 을 만들기 위해 노드 본문에 추가 wrapping context manager 가 필요해 SPEC-AGENT-001
"노드 본문 무변경" 원칙과 충돌한다.

### Clarify cards spans (REQ-OBS-TRACE-CLARIFY-001)

Clarify 카드는 두 turn 에 걸쳐 동작 (SPEC-CLARIFY-CARDS-001):

- Turn N: `ask_clarify` 노드가 inline keyboard 카드 전송 → END
- Turn N+1: 사용자가 버튼 탭 → `clarify:*` callback → `apply_clarify` → `search` → ...

두 turn 은 **서로 다른 webhook**, 따라서 **서로 다른 root trace**. 그러나 `session_id` (sha256(chat_id))
는 동일하므로 Langfuse UI 에서 같은 session 으로 묶여 표시된다. 본 SPEC 은 추가 cross-turn linking
은 요구하지 않는다 (deferred — 만약 필요해지면 Langfuse trace property `trace_id` 를 next turn 으로
전달하는 메커니즘이 별도 SPEC).

---

## Requirements & Acceptance Criteria

### REQ Index

| REQ-ID | Title | Priority |
|---|---|---|
| REQ-OBS-LIB-001 | Pin `langfuse>=3,<4`; main package bundles `langfuse.langchain` | P0 |
| REQ-OBS-MIGRATION-001 | Remove all v2-era fallback code byte-clean | P0 |
| REQ-OBS-CALLBACK-001 | `build_callback_handler()` returns working v3 handler when keys present | P0 |
| REQ-OBS-CALLBACK-002 | Handler `None` is a transparent no-op end-to-end | P0 |
| REQ-OBS-DECORATOR-001 | `observe` decorator collapses to v3 single path (`from langfuse import observe`) | P0 |
| REQ-OBS-TRACE-NODE-001 | All 12 LangGraph nodes emit one entry span each | P0 |
| REQ-OBS-TRACE-LOOP-001 | Reflexion loop renders as sibling spans with `iteration` metadata | P0 |
| REQ-OBS-TRACE-CLARIFY-001 | Clarify card emit + consume produce two traces sharing `session_id` | P0 |
| REQ-OBS-PII-001 | `_hash_for_span` is the ONLY path raw user IDs become span identifiers | P0 |
| REQ-OBS-METADATA-001 | Root trace metadata contains `lang`, `flow`, `critique_retry_count`, `chat_id_hash` | P0 |
| REQ-OBS-COST-001 | < 5ms p99 overhead vs Langfuse-disabled baseline | P0 |
| REQ-OBS-COST-002 | Selective-decoration fallback when p99 budget exceeded | P0 |
| REQ-OBS-FALLBACK-001 | Missing keys / SDK import failure / startup unreachable host → no-op fallback | P0 |
| REQ-OBS-FALLBACK-002 | Mid-session host failure absorbed by SDK retry, no per-webhook ERROR log spam | P0 |

### Library & Migration (REQ-OBS-LIB-*, REQ-OBS-MIGRATION-*)

#### REQ-OBS-LIB-001 — Pin `langfuse>=3,<4`; main package bundles `langfuse.langchain` [P0]

**THE SYSTEM SHALL** declare `langfuse>=3,<4` in `pyproject.toml`'s main dependency group. The v2
`langfuse` pin SHALL be removed. **No separate `langfuse-langchain` distribution is added** —
context7 + GitHub source verification (langfuse-python v3.4.0 `langfuse/__init__.py`,
`langfuse/langchain/__init__.py`) confirms the langchain integration ships as a sub-module of the
main `langfuse` package; no PyPI package named `langfuse-langchain` exists.

**Acceptance**:

- `pyproject.toml` declares exactly one `langfuse` line, pinned `>=3,<4`. No `langfuse-langchain`
  line is added.
- `uv.lock` resolves to a `langfuse` package version `>= 3.0.0` and `< 4.0.0`.
- `uv tree | grep -E '^langfuse'` shows exactly one entry (no v2 dual-pin, no separate
  `langfuse-langchain`).
- `from langfuse import observe` succeeds at import time on a fresh `uv sync` (verified path —
  v3.4.0 `langfuse/__init__.py` exports `observe` in `__all__`).
- `from langfuse.langchain import CallbackHandler` succeeds at import time (verified path —
  `langfuse/langchain/__init__.py` re-exports `LangchainCallbackHandler` as `CallbackHandler`).
- `from langfuse import get_client` succeeds and `get_client().flush()` is callable (verified API —
  `langfuse/_client/client.py` defines `flush`).

#### REQ-OBS-MIGRATION-001 — v2 fallback code SHALL be removed byte-clean [P0]

**THE SYSTEM SHALL** remove all v2-era fallback branches from `app/observability/langfuse.py`:

1. The `try: from langfuse.decorators import observe / except ImportError` branch SHALL be deleted.
2. The `_lf_observe_v2` / `_lf_observe_v3` cascade SHALL collapse to a single `try v3 / fallback
   no-op` path.
3. The `try: from langfuse.callback import CallbackHandler` v2 import in `build_callback_handler`
   SHALL be replaced with the v3 path (`from langfuse.langchain import CallbackHandler` or as
   documented in `plan.md`).
4. The long comment block explaining the v2-vs-langchain-1.x incompatibility SHALL be removed (or
   replaced with a short pointer to this SPEC's HISTORY entry).

**Acceptance**:

- A diff of `app/observability/langfuse.py` between this SPEC's start and end state shows the v2
  import branches removed, no orphan `from langfuse.decorators` references remain anywhere in `app/`,
  no orphan `from langfuse.callback` references remain.
- `grep -R "langfuse.decorators" app/ tests/` returns no hits.
- `grep -R "langfuse.callback" app/ tests/` returns no hits (the v3 callback handler lives at
  `langfuse.langchain` or equivalent — `plan.md` confirms).
- `ruff check .` and `ruff format --check .` pass with no new warnings.
- `mypy app/observability/langfuse.py` (if configured — currently project uses ruff only, mypy is
  optional) reports no unused-import or unreachable-code findings.

---

### Callback Handler (REQ-OBS-CALLBACK-*)

#### REQ-OBS-CALLBACK-001 — `build_callback_handler()` SHALL return a working v3 handler when keys present [P0]

**WHEN** `LANGFUSE_PUBLIC_KEY` AND `LANGFUSE_SECRET_KEY` are both set in the environment,
**AND** the langfuse v3 package is installed,
**THE SYSTEM SHALL** return an instance of `langfuse.langchain.CallbackHandler` (confirmed v3
import path — v3.4.0 source verified, no `plan.md` spike required) populated with `session_id`,
`user_id`, `metadata` from the call kwargs. The handler instance SHALL be valid for use in
`graph.ainvoke(state, config={"callbacks": [handler]})`.

**WHEN** any of the above conditions are false (keys absent, package missing, host unreachable
at handler construction time),
**THE SYSTEM SHALL** return `None` AND emit one `WARNING` level log line with the reason. The
caller passes `None` through into `config["callbacks"]` which langchain treats as no-op.

**Acceptance**:

- An integration test with both keys set asserts `build_callback_handler(session_id="abc",
  user_id="def", metadata={"flow": "image"})` returns a non-None object whose class is
  `langfuse.langchain.CallbackHandler` (verified import path).
- A unit test with keys unset asserts the function returns `None` and emits a WARNING log line
  matching the documented format.
- A unit test simulating an import failure of `langfuse.langchain` (via `sys.modules` patching)
  asserts the function returns `None` and emits a WARNING log line that mentions the import
  failure.
- The handler's `session_id` / `user_id` SHALL receive the pre-hashed values (raw chat_id is
  NEVER passed in — caller is responsible for hashing via `_hash_for_span`). The function's
  signature SHALL document this contract explicitly in its docstring.
- The handler instance MAY be cached / reused across handler invocations within a single webhook
  (since handler carries per-trace context, NOT module-level cache). `plan.md` confirms the
  caching strategy.

#### REQ-OBS-CALLBACK-002 — Handler `None` SHALL be a transparent no-op [P0]

**WHEN** `build_callback_handler(...)` returns `None`,
**THE SYSTEM SHALL** still execute the LangGraph turn to completion without raising any exception.
`config={"callbacks": [None]}` SHALL behave equivalently to `config={"callbacks": []}` — the bot
must NOT fail to serve a webhook just because Langfuse is unreachable.

**Acceptance**:

- An integration test patches `build_callback_handler` to return `None` and runs a full webhook
  flow (photo → vision → search → respond) end-to-end. Asserts: no exception raised, response
  delivered to the channel adapter mock, `OutputState.sent_count >= 1`.
- The caller in `app/api/webhooks/telegram.py` SHALL handle `None` by passing `[]` (empty list)
  into `config.callbacks` rather than `[None]`, to avoid any per-langchain-version quirks. This is
  encoded in `plan.md`.

---

### Decorator (REQ-OBS-DECORATOR-*)

#### REQ-OBS-DECORATOR-001 — `observe` decorator SHALL use v3 API single path [P0]

**THE SYSTEM SHALL** import `observe` from `langfuse` (v3 top-level export) in
`app/observability/langfuse.py`. The current v3 → v2 → no-op three-way cascade SHALL collapse to a
two-way v3 → no-op cascade. The exported `observe(...)` function SHALL maintain its current public
signature `(name: str | None = None, **kwargs) -> Callable[[Callable[P, Awaitable[R]]],
Callable[P, Awaitable[R]]]` so that every existing caller (SPEC-MEMORY-001 memory modules,
`app/pipeline/runner.py`) continues to work without source changes.

**Acceptance**:

- `app/observability/langfuse.py` contains exactly one `from langfuse import observe as
  _lf_observe` line (confirmed v3 canonical import — v3.4.0 `langfuse/__init__.py` `__all__`).
- `from langfuse.decorators` references are removed (REQ-OBS-MIGRATION-001).
- The exported `observe(...)` function from `app/observability/langfuse.py` is invokable as
  `@observe(name="memory.session.update", as_type="span")` AND `@observe(name="pipeline.run")` AND
  `@observe()` (no args). All three forms work.
- A unit test asserts that with Langfuse disabled (keys absent), `@observe(name="x")` is a
  transparent no-op — wrapped async function returns the same value with negligible overhead.
- A unit test asserts that with Langfuse enabled (keys present, host reachable), `@observe(name=
  "x")` produces a span observable via the Langfuse mock client.

---

### Node-Level Tracing (REQ-OBS-TRACE-*)

#### REQ-OBS-TRACE-NODE-001 — Every LangGraph node SHALL produce an entry span [P0]

**THE SYSTEM SHALL** decorate the entry function of every node in `app/graphs/nodes/` with
`@observe(name="node.{node_name}", as_type="span")` so that every node invocation produces exactly
one Langfuse span. The 12 nodes are: `ingest`, `resolve_image`, `vision`, `pick_item`,
`ask_clarify`, `apply_clarify`, `critique_apply`, `search`, `evaluator`, `send_results`,
`taste_update`, `respond`.

**Acceptance**:

- A `grep -R "@observe" app/graphs/nodes/` returns at least 12 hits — one per node entry function.
- An integration test with a mock Langfuse client exercises a webhook that hits all 12 nodes (or
  the maximum reachable in one turn — typically 7–9: ingest → vision → pick_item → critique_apply →
  search → evaluator → send_results → respond) and asserts each visited node emits one span.
- Span names follow the convention `node.{node_name}` (lowercase, dot-separated). Exact constants
  defined in `plan.md`.
- A node that exits early (e.g., `ingest` rejecting an invalid update) STILL emits its entry span.
  The `@observe` decorator captures function invocation, not function success.
- Each span's metadata SHALL include the node-specific attributes documented in the "Trace Shape
  Reference" table above (e.g., `flow` on `ingest`, `axis_chosen` on `ask_clarify`,
  `items_count` on `pick_item`).
- The decorator order matters when a node already has other decorators (e.g., some nodes have
  `@dataclass`-style helpers or `@cache` — currently none, but `plan.md` documents the convention:
  `@observe(...)` is the outermost decorator on async node functions).
- **Partial-decoration fallback (REQ-OBS-COST-002)**: if REQ-OBS-COST-001 's < 5ms p99 budget is
  exceeded after full decoration, REQ-OBS-COST-002 governs the reduced-coverage rollback. This
  REQ's "ALL 12 nodes" requirement is satisfied by either (i) all 12 decorated under the budget,
  OR (ii) the LLM-calling subset (`vision`, `critique_apply`, `evaluator`, `respond`) decorated
  with the other 8 nodes' span emission deferred and the deferral documented in
  `.moai/specs/SPEC-OBSERVABILITY-002/progress.md` per REQ-OBS-COST-002.

#### REQ-OBS-TRACE-LOOP-001 — Reflexion loop SHALL render as parent–child tree with retry-indexed children [P0]

**WHEN** the Reflexion loop (`search → evaluator → critique_apply → search`) runs N+1 iterations
(N retries),
**THE SYSTEM SHALL** produce a trace tree where the root webhook trace has exactly N+1 sibling
`node.search` spans and N+1 sibling `node.evaluator` spans, each `evaluator` span carrying the
`iteration` metadata attribute (0-indexed) so that the retry sequence is reconstructable from the
trace.

**Acceptance**:

- An integration test mocks a Reflexion scenario where `evaluator` returns `score=0.4, retry=True`
  on iteration 0, `score=0.55, retry=True` on iteration 1, `score=0.7, retry=False` on iteration 2.
  Asserts the trace tree contains: 3 `node.search` spans, 3 `node.evaluator` spans, each evaluator
  span's metadata contains `iteration` equal to its index (0, 1, 2).
- The integration test also asserts that the metadata `retry_count` on the ROOT webhook trace
  equals 2 (matches `WorkingState.critique_retry_count` at graph completion).
- This SPEC explicitly chooses **option A (sequential sibling spans)** over option B (synthetic
  `loop.reflexion` parent span) per the "Trace Shape Reference" section's reasoning. `plan.md`
  documents the decision.
- The metadata attached to each `evaluator` span MUST include the keys from SPEC-AGENTIC-CRITIQUE-001
  REQ-CRITIQUE-OBSV-001 (`score`, `reasoning`, `retry`, `retry_count`, `suggested_delta_summary`,
  `candidates_count_in`, `candidates_count_out`, `source`, `evaluator_model`, `elapsed_ms`) — these
  are already emitted by `evaluator.py` per that prior SPEC; this SPEC requires they appear on the
  Langfuse span via the **v3 client API `langfuse.update_current_span(metadata={...})`** invoked
  inside the node body. Rationale: keys like `score` / `retry_count` / `elapsed_ms` are computed
  during the function (not before), so the static `metadata=...` kwarg on the `@observe` decorator
  cannot carry them. The v3 client (`langfuse/_client/client.py`) exposes `update_current_span`,
  `update_current_trace`, `update_current_generation` — confirmed by source inspection. `iteration`
  attribute is added the same way at the start of the function (after `iteration` is known).
  v2's `langfuse_context.update_current_observation` does NOT exist in v3 — do not use it.

#### REQ-OBS-TRACE-CLARIFY-001 — Clarify card emission and consumption SHALL produce two spans linked by `session_id` [P0]

**WHEN** turn N's `ask_clarify` node emits an inline keyboard card,
**AND** turn N+1's `apply_clarify` node consumes the resulting `clarify:*` callback,
**THE SYSTEM SHALL** produce two separate root traces (one per webhook), each containing exactly one
`node.ask_clarify` span (turn N) or one `node.apply_clarify` span (turn N+1). Both traces SHALL share
the same `session_id` (`sha256(chat_id)[:16]`) so that the Langfuse UI naturally groups them under
one session view.

**Acceptance**:

- An integration test plays both webhooks in sequence against a mock Langfuse client and asserts:
  (a) two distinct traces exist, (b) both have the same `session_id`, (c) turn N's trace contains
  one `node.ask_clarify` span with metadata `axis_chosen=...`, (d) turn N+1's trace contains one
  `node.apply_clarify` span with metadata `axis=...`, `value=...`, `boost_keywords_added=...`.
- No additional cross-trace linking mechanism is required by this SPEC; Langfuse's session view
  groups by `session_id` automatically. Future SPEC may add explicit `trace_id` chaining if needed.

---

### PII Handling (REQ-OBS-PII-*)

#### REQ-OBS-PII-001 — `session_id` and `user_id` SHALL be sha256-prefix-16 hashes [P0]

**THE SYSTEM SHALL** introduce a single helper in `app/observability/langfuse.py`:

```python
def _hash_for_span(value: int | str | None) -> str | None:
    """Return sha256(str(value))[:16] or None when input is None."""
```

This helper SHALL be the ONLY mechanism by which `chat_id` and `from_user_id` (or any other raw
user identifier) are converted to span identifiers. Every caller of `observe(...)` or
`build_callback_handler(...)` that needs to pass `session_id` / `user_id` / `chat_id_hash` SHALL
route through this helper. The raw integer / string `chat_id` and `from_user_id` SHALL NOT appear
in any span attribute, metadata dict, or log line emitted from any module imported by the trace
machinery.

**Acceptance**:

- A unit test calls `_hash_for_span(12345)` twice and asserts the two return values are equal and
  match the hex digest of `sha256("12345").hexdigest()[:16]`.
- A unit test calls `_hash_for_span(None)` and asserts the return value is `None`.
- A unit test calls `_hash_for_span("12345")` and asserts the result equals `_hash_for_span(12345)`
  (same sha256 source string).
- A static analyzer (`tests/test_observability/test_pii.py`) greps `app/observability/`,
  `app/api/webhooks/`, and `app/graphs/` for the patterns `chat_id` and `from_user_id` appearing
  as values (not just key names) in any string literal that could plausibly be a Langfuse metadata
  payload, and asserts every occurrence is either (a) inside `_hash_for_span(...)` call, or
  (b) explicitly excluded by a comment. False-positive-prone but catches naive `f"chat={chat_id}"`
  in log lines that would leak.
- A dynamic test exercises a full webhook with `chat_id=999999999` and `from_user_id=888888888`,
  captures all span emissions to a mock Langfuse client, and asserts the literal strings
  `"999999999"` and `"888888888"` do NOT appear anywhere in the captured payload (recursive scan
  over dict values, list elements, and string fields).
- This REQ extends (does not replace) SPEC-AGENT-001 REQ-OBSV-005, which mandated the same
  sha256-prefix-16 rule but did not enforce a single helper.

---

### Metadata (REQ-OBS-METADATA-*)

#### REQ-OBS-METADATA-001 — Root trace SHALL carry standard metadata fields [P0]

**THE SYSTEM SHALL** populate every webhook root trace's metadata with at minimum the following
fields:

| Key | Type | Source |
|---|---|---|
| `lang` | `str` | `Session.lang` at graph completion (`"ko"` or `"en"`) |
| `flow` | `str` | One of `"image"`, `"text"`, `"callback"` based on inbound message shape — exact classifier defined in `plan.md` |
| `critique_retry_count` | `int` | Final `WorkingState.critique_retry_count` at graph completion (typically 0; > 0 when Reflexion retried) |
| `chat_id_hash` | `str` | Same value as `session_id` — duplicated for searchability in Langfuse UI |

Per-node spans MAY add their own metadata keys (documented in the "Trace Shape Reference" table)
but the four root-level keys above are REQUIRED on every webhook trace.

**Acceptance**:

- An integration test exercising an image-flow webhook asserts the root trace's metadata contains
  all four keys with the documented values.
- An integration test exercising a text-only callback flow asserts `metadata.flow == "callback"`
  and `metadata.critique_retry_count == 0` (no retries triggered for a simple `crit:*` tap that
  re-uses cached results).
- A test exercising a Reflexion 2-retry scenario asserts `metadata.critique_retry_count == 2` on
  the root trace.
- The `lang` value follows the SPEC-AGENT-001 sticky-language semantics: it reflects the language
  used in the bot's REPLY for that turn, not necessarily the language of the user's input. This
  ensures Langfuse session-level language filtering matches what the user actually saw.

---

### Cost & Latency (REQ-OBS-COST-*)

#### REQ-OBS-COST-001 — End-to-end webhook flow SHALL add < 5ms p99 overhead vs Langfuse-disabled baseline [P0]

**THE SYSTEM SHALL** ensure the per-webhook latency overhead added by Langfuse v3 tracing is bounded
as below. **Baseline definition**: the same post-SPEC build (12 decorated nodes, v3 SDK installed,
`build_callback_handler` wired into `graph.ainvoke`) running with `LANGFUSE_PUBLIC_KEY=""` so the
entire cascade collapses to no-op. The only delta between baseline and measurement is the
keys-unset vs keys-set environment — same code path, same decorator instances, same handler-
construction call site. This eliminates circularity: we are NOT comparing "old code without
decorators" to "new code with decorators"; we are comparing "decorators inert" to "decorators
active".

| Percentile | Threshold |
|---|---|
| p50 | < 1ms |
| p95 | < 3ms |
| p99 | < 5ms |

Measured end-to-end across the webhook handler entry to the channel adapter's `sendMessage` call.

**Acceptance**:

- A benchmark test in `tests/test_observability/test_latency.py` runs 1000 iterations of a synthetic
  webhook (mocked Telegram update, mocked Vision LLM response, mocked search RPC, mocked LLM
  responses) under two configurations of the SAME post-SPEC binary:
  1. **Baseline**: `LANGFUSE_PUBLIC_KEY=""` (and/or `LANGFUSE_SECRET_KEY=""`). The cascade in
     `app/observability/langfuse.py` resolves to no-op decorator + `build_callback_handler` returns
     `None`. Hot path executes the same decorators and handler-creation call site as configuration 2.
  2. **Measurement**: Both keys set to valid-looking values, v3 SDK initialized, pointed at a local
     mock Langfuse server (response time ≈ 0). Decorators record spans; SDK enqueues for background
     send.
  Asserts the delta in p50 / p95 / p99 wall-clock latency between the two configurations is below
  the thresholds above.
- The benchmark SHALL use `time.perf_counter_ns()` for timing precision.
- The benchmark SHALL be deterministic enough to be run in CI; flakiness budget is documented in
  `plan.md`.
- The Langfuse v3 SDK's background queue + flush model is what makes this achievable — the hot
  path only enqueues spans, the actual network send happens asynchronously. `plan.md` confirms by
  reference to the v3 client architecture documentation.
- The `app/main.py` lifespan exit SHALL call `get_client().flush()` (confirmed v3 API — v3.4.0
  `langfuse/_client/client.py`) so the background queue is drained on graceful shutdown. This is
  verified in REQ-OBS-FALLBACK-001's startup/shutdown test scenarios.

#### REQ-OBS-COST-002 — Selective-decoration fallback when p99 budget exceeded [P0]

**WHEN** the benchmark test required by REQ-OBS-COST-001 shows the measured p99 overhead exceeds
**5ms** (the budget threshold),
**THE SYSTEM SHALL** drop `@observe` decoration from the 8 non-LLM-calling nodes (`ingest`,
`resolve_image`, `pick_item`, `ask_clarify`, `apply_clarify`, `search`, `send_results`,
`taste_update`) while keeping it on the 4 LLM-calling nodes (`vision`, `critique_apply`,
`evaluator`, `respond`). This is the documented fallback to preserve the cost envelope.

**Rationale**: ALL-12 decoration is the preferred shape (full trace completeness). But the SPEC
cannot promise zero-overhead in the face of unknown SDK enqueue cost behavior. R4 in Risks &
Mitigations names this as a real risk; this REQ encodes the response with an explicit threshold
and rollback procedure rather than a vague "fall back".

**Acceptance**:

- The benchmark test produces a `latency-baseline.json` artifact recording p50/p95/p99 for both
  baseline and measurement configurations. If p99-delta > 5ms, the test SHALL fail and emit a
  recommendation to invoke REQ-OBS-COST-002 fallback.
- Rollback procedure (executed only if REQ-OBS-COST-001 fails):
  1. Remove `@observe` from 8 non-LLM nodes in a follow-up commit on the same SPEC branch.
  2. Re-run benchmark; verify p99-delta ≤ 5ms.
  3. Append entry to `.moai/specs/SPEC-OBSERVABILITY-002/progress.md` titled
     `REQ-OBS-COST-002 invoked` with: measured p99 (full), measured p99 (reduced), list of nodes
     stripped, date.
  4. Update REQ-OBS-TRACE-NODE-001 acceptance language to reflect partial-coverage (the
     "ALL 12" requirement is downgraded to "all 4 LLM-calling nodes + as many non-LLM as fit the
     budget").
  5. Update HISTORY entry with `selective_mode` activation note.
- A unit test asserts the existence of a `LANGFUSE_SELECTIVE_MODE` feature flag (default `false`)
  that when set to `true` causes the 8 non-LLM `@observe` decorations to no-op at import time
  (zero cost). This makes the fallback toggleable via env without code change in an emergency.
- The full-decoration default SHALL be restored in a later SPEC if SDK improvements bring overhead
  back under budget. No automatic re-activation; explicit human decision.

---

### Fallback (REQ-OBS-FALLBACK-*)

#### REQ-OBS-FALLBACK-001 — Missing keys OR unreachable host SHALL degrade to no-op without aborting startup [P0]

**WHEN** `LANGFUSE_PUBLIC_KEY` is empty OR `LANGFUSE_SECRET_KEY` is empty,
**THE SYSTEM SHALL** initialize `app/observability/langfuse.py` in no-op mode: `observe(...)` is
the transparent passthrough, `build_callback_handler(...)` returns `None`, no network connection
to Langfuse host is attempted.

**WHEN** keys are present BUT the v3 SDK fails to import (e.g., wrong package version on disk),
**THE SYSTEM SHALL** also fall back to no-op mode AND emit ONE `ERROR` level log line at module
import time explaining the import failure. Bot startup SHALL proceed.

**WHEN** keys are present, SDK imports cleanly, but the Langfuse host is unreachable on first send,
**THE SYSTEM SHALL** let the v3 SDK's internal retry/backoff handle it (the SDK is non-blocking by
design — host-unreachable does NOT propagate to the hot path). The bot SHALL continue to serve
webhooks. The SDK's own logger will emit retry warnings; we do NOT add additional retry logic.

**Acceptance**:

- A startup test with `LANGFUSE_PUBLIC_KEY=""` asserts the FastAPI app initializes cleanly, `/health`
  returns 200, and `build_callback_handler` returns `None`.
- A startup test with `LANGFUSE_PUBLIC_KEY` set to a valid-looking key but `LANGFUSE_HOST` set to
  `http://127.0.0.1:1` (unreachable) asserts the FastAPI app initializes cleanly. The SDK's own
  retry behavior is OUT OF SCOPE for our tests — we trust the SDK contract.
- A test simulating an import failure (via `sys.modules` patch making `import langfuse` raise
  `ImportError`) asserts the module's `_ENABLED` falls back to `False` and one ERROR log line is
  emitted.
- The fallback path SHALL be one-shot at module import time; once `_ENABLED=False`, the system
  does NOT attempt mid-flight reconnection. Matches the existing pattern. SPEC-MEMORY-001's
  `MEMORY_FALLBACK_ON_PROBE_FAIL` is parallel logic for a different subsystem.
- The fallback path SHALL NOT emit additional ERROR logs per webhook (would flood logs on a
  Langfuse outage). The one-shot ERROR at import + the SDK's own internal logging is sufficient.

#### REQ-OBS-FALLBACK-002 — Mid-session host failure SHALL be absorbed by SDK retry without hot-path impact [P0]

**WHEN** the Langfuse host becomes unreachable AFTER successful startup (i.e., the SDK initialized
cleanly, the first N webhooks traced fine, then the host goes down — network partition, server
restart, hosted-service outage),
**THE SYSTEM SHALL** rely on the v3 SDK's built-in non-blocking background queue + retry/backoff
to absorb the failure. The webhook hot path SHALL remain unaffected: span enqueue is a local
in-memory operation that does NOT touch the network. The bot SHALL continue to serve recommendations,
clarify cards, and respond messages with byte-identical observable behavior. No per-webhook ERROR
log line SHALL be emitted by `app/observability/langfuse.py` for the host-unreachable condition.

**Acceptance**:

- An integration test starts the FastAPI app with valid keys and a mock Langfuse server, runs 10
  successful webhooks (each producing a captured trace), then kills the mock server, then runs
  another 10 webhooks. Asserts: (a) all 20 webhooks return responses to the channel adapter,
  (b) zero unhandled exceptions are raised in the application code, (c) the only mid-session log
  records from `app/observability/langfuse.py` are at DEBUG level or lower (NOT ERROR/WARNING for
  the host-unreachable condition specifically — SDK's own loggers may emit retry messages, which
  is OUT OF SCOPE).
- The integration test SHALL NOT assert that the 11–20th traces eventually flush; the SDK's
  retry policy and queue-overflow behavior is the SDK's responsibility (see R7). Span loss on
  extended outage is acceptable; webhook latency degradation is NOT.
- The test SHALL also verify: when the mock server is restored, subsequent webhook traces resume
  flushing successfully (SDK's automatic recovery). NO module-level state reset SHALL be required —
  no `_ENABLED` flag toggle, no re-init of `get_client()`.
- This REQ extends REQ-OBS-FALLBACK-001 (which covers startup-time failures) to cover the
  steady-state failure mode that is more common in production. Together, the two REQs cover the
  full lifecycle: (a) keys absent at startup, (b) SDK import failure, (c) host unreachable at first
  send (= startup-time), (d) host failure mid-session (this REQ).

---

## Environment Variables (consumed, not introduced)

| Var | Required | Default | Description |
|---|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | no | `""` | Langfuse v3 public key. Empty → no-op fallback. REQ-OBS-FALLBACK-001. |
| `LANGFUSE_SECRET_KEY` | no | `""` | Langfuse v3 secret key. Empty → no-op fallback. REQ-OBS-FALLBACK-001. |
| `LANGFUSE_HOST` | no | (SDK default) | Self-host URL (already pointed at dev-app's Langfuse instance). Unchanged. |

**THIS SPEC INTRODUCES ZERO NEW ENV VARIABLES.** All three variables above already exist and are
populated in `.env` for the dev environment. The migration from v2 to v3 SDK uses the same keys.

---

## Non-Goals (out of scope for this SPEC)

The following are explicitly NOT delivered by SPEC-OBSERVABILITY-002 and MUST NOT be conflated with it:

1. **Langfuse self-host operations.** Deploying, scaling, backing up, monitoring, or upgrading the
   Langfuse server instance running on dev-app EC2. That is owned by aws-infra
   (`aws-infra/kiko-ai-servers/portal-ai/`).
2. **LangSmith or alternative tracing backends.** Langfuse is the chosen backend. No multi-backend
   abstraction.
3. **Trace export to BigQuery / Snowflake / Athena.** Langfuse-internal storage suffices for current
   needs. Future SPEC if analytics needs grow.
4. **Custom Langfuse dashboards or alerts.** The Langfuse UI's built-in views are sufficient. We do
   not author custom Grafana panels or alert rules in this SPEC.
5. **A new evaluator that scores Langfuse traces** (different from the SPEC-AGENTIC-CRITIQUE-001
   `evaluator` LangGraph node which scores SEARCH RESULTS, not traces). LLM-as-judge over historical
   traces is a separate roadmap item.
6. **Cost budget alerts beyond Langfuse-internal.** No PagerDuty, no Slack hook, no email — Langfuse
   UI's own cost views are the source of truth. Deferred until volume justifies.
7. **Cross-turn trace linking** via explicit `trace_id` propagation. Same-session traces group via
   `session_id`; explicit chaining is a future SPEC if needed.
8. **Synthetic `loop.reflexion` parent span.** Option B in the "Trace Shape Reference" section was
   considered and rejected. Sequential sibling spans (option A) is the chosen shape.
9. **Server-side Langfuse upgrade.** The dev-app self-host instance is already on a v3-compatible
   version per deployment confirmation. We do NOT touch the server. If a server upgrade later
   becomes necessary, it is an aws-infra SPEC.
10. **Modifying SPEC-AGENT-001, SPEC-AGENTIC-CRITIQUE-001, SPEC-CLARIFY-CARDS-001, SPEC-MEMORY-001,
    SPEC-VISION-UNIFY-001, SPEC-PIPELINE-001, or SPEC-MSG-001.** Their requirements (REQ-OBSV-*,
    REQ-CRITIQUE-OBSV-*, REQ-CLARIFY-OBSV-*, REQ-MEMORY-OBS-*, REQ-VISION-OBSV-*) describe what
    metadata each node emits; this SPEC describes how those emissions become real Langfuse spans
    once the v3 callback handler works. No prior SPEC's requirements are renumbered or rewritten.
11. **Adding new env vars.** All three Langfuse env vars (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
    `LANGFUSE_HOST`) already exist and are populated.
12. **Changes to node body logic.** The 12 nodes' business logic stays byte-identical. Only the
    `@observe` decorator is added at the entry function. (The no-op fallback path also survives
    so local dev / CI runs without Langfuse keys still work — REQ-OBS-FALLBACK-001.)
13. **Touching `app/pipeline/runner.py` semantically.** The existing `@observe(...)` on
   `run_pipeline` continues to work as-is once v3 is active; this SPEC does NOT rewrite pipeline
    tracing.
14. **Adding tracing to channel transport** (`app/channels/telegram/adapter.py`). The adapter's
    HTTP calls to Telegram are out of scope; if needed later, separate SPEC.
15. **A per-user opt-out mechanism for tracing.** GDPR / "do not trace me" is deferred to a future
    privacy SPEC. Current dev environment has no production users.
16. **Trace sampling.** All webhooks produce traces; sampling logic (e.g., 1-in-10) is deferred
    until volume justifies.
17. **Migrating the four LiteLLM-calling modules to langchain Runnable wrappers.** SPEC-AGENT-001
    Non-Goal #6 already excludes this. The wrap-LiteLLM-in-langchain idea would let the callback
    handler automatically capture LiteLLM calls without `@observe`, but is high-risk for cost/latency
    regression. We stick with `@observe` on those modules.

---

## Exclusions (What NOT to Build)

(Mirrors Non-Goals — explicit list for SPEC-checker compliance.)

1. No Langfuse server-side operational work (deploy, scale, backup, monitor).
2. No LangSmith or alternative tracing backend.
3. No trace export pipeline to external data warehouses.
4. No custom dashboards or alerts beyond Langfuse UI defaults.
5. No LLM-as-judge evaluator over historical traces.
6. No external cost-budget alerting (PagerDuty / Slack / email).
7. No cross-turn explicit `trace_id` chaining.
8. No synthetic `loop.reflexion` parent span (sibling spans only).
9. No Langfuse server upgrade in this SPEC.
10. No modification of prior SPECs' requirements (REQ-OBSV-*, REQ-CRITIQUE-OBSV-*, REQ-CLARIFY-OBSV-*,
    REQ-MEMORY-OBS-*).
11. No new environment variables.
12. No node business-logic changes (no-op fallback path survives v3 activation per REQ-OBS-FALLBACK-001).
13. No semantic rewrite of `app/pipeline/runner.py`.
14. No tracing of channel transport (`adapter.py`).
15. No per-user tracing opt-out.
16. No trace sampling logic.
17. No migration of LiteLLM-calling modules to langchain Runnable wrappers.

---

## Stakeholders

| Role | Responsibility |
|---|---|
| Product / Founder (hchsa77@gmail.com) | Approves the < 5ms p99 latency budget, the trace-shape decision (option A: sequential sibling spans for Reflexion loop), and the no-server-side-change scope. |
| AI Server Owner (this SPEC) | All work in `app/observability/langfuse.py`, `app/main.py` lifespan flush, `app/api/webhooks/telegram.py` callback wiring, all 12 `app/graphs/nodes/*.py` decorator additions, `pyproject.toml` v3 pin, `uv.lock` regeneration, all tests in `tests/test_observability/`. Owns the migration PR. |
| Langfuse operator (aws-infra) | Verifies the dev-app Langfuse self-host instance is on a v3-compatible server version BEFORE this PR merges. Provides confirmation. Out-of-scope for this PR's diff. |
| Modal / kikoai/app teams | Out of scope. Tracing is internal to kikoai/ai's request path. |
| Future SPEC owners | This SPEC's `@observe` decorations on every node + the working v3 callback handler are prerequisite for any future SPEC that wants to add LLM-as-judge over historical traces, dashboard alerts, or sampling. |

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **v3 server-side incompatibility**. The dev-app Langfuse self-host instance turns out to be on a v2-only server version and the v3 client's wire format is rejected. | Low | High | Pre-PR check with Langfuse operator confirms server version. If a server upgrade is required, this SPEC is paused until aws-infra delivers it. Documented in Stakeholders. |
| R2 | **v3 SDK introduces breaking API change in `observe(...)` signature** that propagates to SPEC-MEMORY-001 callers (the four memory-module decorations). | Medium | Medium | `app/observability/langfuse.py` exports `observe` via the project-local wrapper that maintains the documented signature `(name, **kwargs)`. v3 → v2 cascade collapse SHALL preserve that contract. A regression test imports `from app.observability.langfuse import observe` and exercises every existing caller pattern. |
| R3 | **`langfuse.langchain.CallbackHandler` import path is different from documented assumption** (e.g., it's `from langfuse_langchain import CallbackHandler` instead). | Low (mitigated) | Low | RESOLVED via context7 + v3.4.0 source inspection: `from langfuse.langchain import CallbackHandler` is the canonical path, bundled in main `langfuse` package. REQ-OBS-LIB-001 acceptance encodes this. No `plan.md` spike needed. |
| R4 | **Adding `@observe` to 12 nodes adds measurable overhead** in the hot path. | Low | Medium | REQ-OBS-COST-001's < 5ms p99 budget is the gate; REQ-OBS-COST-002 names the rollback (drop decoration from 8 non-LLM nodes, keep LLM-calling 4) with explicit threshold and procedure. Feature flag `LANGFUSE_SELECTIVE_MODE` allows emergency env-only toggle. |
| R5 | **PII leakage** via a span attribute we forgot to audit (e.g., a node logs `chat_id` directly into its metadata). | Medium | High | REQ-OBS-PII-001's static + dynamic test combo. The static test grep-walks `app/` for raw `chat_id` / `from_user_id` patterns in suspicious contexts. The dynamic test runs a real webhook with marker IDs and asserts those literal strings do NOT appear in captured span payload. |
| R6 | **`get_client().flush()` on lifespan exit blocks the shutdown** if the queue is large. | Low | Low | The v3 SDK's `flush()` accepts a timeout. `plan.md` sets a 5s timeout — sufficient for normal volume, prevents indefinite block. |
| R7 | **Background queue overflow** in the SDK on burst traffic loses spans. | Low | Medium | The v3 SDK is documented to use a bounded queue with backpressure. On overflow, it drops spans rather than blocking the hot path. Losing the occasional span is preferred over blocking webhook latency. We do NOT add a queue-size config in this SPEC; defer to SDK defaults. |
| R8 | **Schema drift between SPEC-AGENT-001 / SPEC-AGENTIC-CRITIQUE-001 documented metadata keys and what actually lands on Langfuse spans.** | Medium | Low | REQ-OBS-TRACE-LOOP-001 acceptance reads from the prior SPECs' metadata key lists. The integration tests in `tests/test_observability/test_trace_shape.py` exercise representative flows and assert the expected keys are present. |
| R9 | **`uv sync` to upgrade `langfuse` v2 → v3 causes transitive dependency conflicts** (e.g., a transitive package requires v2). | Low | Medium | Resolution is done in a dedicated branch. If conflicts arise, `plan.md` either pins the transitive package to a v3-compatible version or escalates to a separate SPEC if the conflict is unresolvable. |
| R10 | **The migration PR's diff is large** (12 nodes + observability module + tests + lockfile) and risks reviewer fatigue. | Medium | Low | The PR is structurally simple: decorator additions are 1-line changes per node. The non-trivial logic is concentrated in `app/observability/langfuse.py`. PR description summarizes by category. CI's existing test suite + the new `tests/test_observability/` suite is the safety net. |
| R11 | **v3 SDK's tracking of `lang` / `flow` metadata uses a different mechanism than v2's `metadata` kwarg.** | Low (mitigated) | Low | RESOLVED: v3 uses `langfuse.update_current_span(metadata={...})` / `update_current_trace(...)` (per v3.4.0 `langfuse/_client/client.py`). The wrapper in `app/observability/langfuse.py` MAY introduce a tiny helper `update_trace_metadata(**kwargs)` that proxies to `get_client().update_current_trace(metadata=kwargs)` so callers don't bind to the SDK's internals directly. |
| R12 | **Langfuse v3 changes how span hierarchies form in async code** (e.g., `asyncio.gather` siblings end up as flat list instead of nested). | Low | Low | LangGraph 1.x runs nodes sequentially within a single graph invocation (no `asyncio.gather` of nodes), so the issue doesn't arise. Pipeline-internal parallelism (if any) is already covered by SPEC-PIPELINE-001's existing `@observe` decoration. |
| R13 | **The `@observe` decorator interferes with LangGraph's state-delta return pattern** (nodes return `dict` deltas; the decorator might wrap return values). | Medium | High | The v3 `@observe` (like v2) does NOT modify the wrapped function's return value — it only records timing/inputs/outputs as a side effect. Verified by a unit test in `tests/test_observability/test_langfuse.py` that asserts decorated and undecorated versions of the same function return identical dicts. |
| R14 | **CI flakiness** from the benchmark test in REQ-OBS-COST-001 (latency thresholds are tight). | Medium | Low | Threshold is < 5ms p99 — comfortably above SDK enqueue overhead which is typically < 100µs. `plan.md` allows a 20% headroom factor for CI variance (effective threshold 6ms p99). The unit-test-level deterministic benchmark differs from the live-traffic SLO. |
| R15 | **Re-running the existing test suite under the v3 SDK** reveals incompatibilities (e.g., test mocks that assume v2 `langfuse.decorators.observe` import path). | Medium | Low | Existing tests SHALL be updated to import from `app.observability.langfuse` (project-local wrapper), not from `langfuse.decorators` directly. A grep + Edit pass during the migration PR. |

---

## Open Questions (deferred to plan.md / implementation)

OQ-1 (exact v3 langchain CallbackHandler import path) and OQ-2 (sub-package vs separate
distribution) are **RESOLVED** in v0.2.0 via context7 MCP + langfuse-python v3.4.0 GitHub source
inspection: `from langfuse.langchain import CallbackHandler`, bundled in the main `langfuse`
package, no separate distribution. See "Langfuse v3 변경점" section in Background.

OQ-5 (mid-node metadata update mechanism) is also **RESOLVED** in v0.2.0: use the v3 client API
`langfuse.update_current_span(metadata={...})` / `update_current_trace(...)` /
`update_current_generation(...)` from `langfuse/_client/client.py`. v2's `langfuse_context` does
not exist in v3.

Remaining plan-phase Open Questions:

1. **Caching strategy for `build_callback_handler` results.** Per-webhook (one fresh handler per
   `graph.ainvoke`) vs module-level singleton (one handler reused across all webhooks). Per-webhook
   is correct semantically (handler carries trace-context) but more allocation per turn. The
   handler is cheap to construct, so per-webhook is the lean default. `plan.md` confirms.
2. **`metadata.flow` classifier.** Exact rules for assigning `"image"` / `"text"` / `"callback"`
   based on inbound `ChannelMessage` shape — `plan.md` writes the classifier function with
   doc-string mapping every input shape to an output label.
3. **`get_client().flush()` location.** In `app/main.py` lifespan exit (graceful shutdown) AND/OR
   per-webhook (after `graph.ainvoke`)? Per-webhook flush would guarantee no span loss on container
   crash but adds latency. Lifespan-only is the lean default. `plan.md` confirms.
4. **CI benchmark stability.** REQ-OBS-COST-001's < 5ms p99 requires either a stable CI environment
   or a per-run baseline. `plan.md` decides between (a) absolute threshold (simpler, may be flaky)
   or (b) relative threshold ("≤ 110% of disabled baseline") and picks the more stable option.

---

## Cross-References

- **Builds on**:
  - SPEC-MSG-001 (channel transport — unchanged).
  - SPEC-AGENT-001 (LangGraph topology + `build_callback_handler` site that this SPEC actually
    activates; REQ-OBSV-001 through REQ-OBSV-005 are the metadata contracts this SPEC realizes).
  - SPEC-AGENTIC-CRITIQUE-001 (`evaluator` node metadata keys from REQ-CRITIQUE-OBSV-001 are now
    visible on Langfuse spans).
  - SPEC-CLARIFY-CARDS-001 (`ask_clarify` / `apply_clarify` metadata from REQ-CLARIFY-OBSV-001 is
    now visible on Langfuse spans).
  - SPEC-VISION-UNIFY-001 (Vision v2 schema is what the `vision` node's span attributes describe).
  - SPEC-PIPELINE-001 (`@observe` on `run_pipeline` continues to work; this SPEC just makes the
    spans land in a working trace tree).
  - SPEC-MEMORY-001 (the four `@observe`-decorated memory methods inherit the working trace tree
    automatically with zero memory-module changes).
- **Triggers / unblocks**:
  - Future SPEC: LLM-as-judge over historical traces (now has trace data to evaluate).
  - Future SPEC: Custom Langfuse dashboards / SLO alerts (now has consistent span shapes).
  - Future SPEC: Cross-turn `trace_id` chaining (now has session_id grouping as a stepping stone).
  - Future SPEC: Trace export to BigQuery (now has consistent metadata payload to ETL).
- **Affected modules in kikoai/ai**:
  - REWRITE: `app/observability/langfuse.py` (v3 single-path, `_hash_for_span` helper, working v3
    callback handler).
  - MODIFIED: `app/main.py` (lifespan `langfuse.flush()` on exit), `app/api/webhooks/telegram.py`
    (`observe(name="webhook.telegram", ...)` wrapping the entry + `build_callback_handler` → `config.
    callbacks`), `app/graphs/nodes/{ingest,resolve_image,vision,pick_item,ask_clarify,apply_clarify,
    critique_apply,search,evaluator,send_results,taste_update,respond}.py` (each gets `@observe(name=
    "node.{name}", as_type="span")` on entry function), `app/graphs/nodes/vision.py` and
    `app/graphs/nodes/critique_apply.py` and `app/graphs/nodes/evaluator.py` (additional
    `as_type="generation"` decorator on the LLM-call helper), `pyproject.toml` (`langfuse>=3,<4`),
    `uv.lock` (regenerated).
  - NEW: `tests/test_observability/test_langfuse.py`, `tests/test_observability/test_trace_shape.py`,
    `tests/test_observability/test_pii.py`, `tests/test_observability/test_latency.py`.
  - UNCHANGED (asserted): `app/graphs/state.py`, `app/graphs/routing.py`,
    `app/graphs/fashion_bot.py` (graph structure), `app/channels/session.py`,
    `app/channels/session_pg.py`, `app/channels/taste_profile.py`, `app/channels/taste_profile_pg.py`,
    `app/pipeline/runner.py`, `app/pipeline/state.py`, `app/pipeline/{embed,enhance_query,search,
    diversify}.py`, `app/providers/{llm,embedding,database,db_pool}.py`, `app/channels/{factory,
    adapter,vision,vision_prompt,clarify,clarify_values,lang,link_resolver}.py`,
    `app/channels/telegram/*`, `app/api/{health,recommend}.py`, `app/core/config.py`,
    `.env.example` (no new env vars), all 12 node business-logic bodies (only the decorator on
    the entry function is added; the function body stays byte-identical).
- **Project context**: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md`.
- **Reference**: `app/observability/langfuse.py` (current no-op + v2 cascade — to be rewritten),
  `app/graphs/fashion_bot.py` (`build_callback_handler` call site — currently receives `None`).
- **External docs**: Langfuse v3 release notes, Langfuse Python SDK v3 docs (`langfuse.observe`,
  `langfuse.langchain.CallbackHandler`). `plan.md` cites exact versions and links.

---

## Definition of Done (P0)

- [ ] REQ-OBS-LIB-001 implemented. `pyproject.toml` pins `langfuse>=3,<4`. `uv.lock` resolves to v3.
      `from langfuse import observe` and `from langfuse.langchain import CallbackHandler` (or v3
      canonical import path as confirmed in `plan.md`) both succeed at import time.
- [ ] REQ-OBS-MIGRATION-001 implemented. v2 import branches removed; `grep -R "langfuse.decorators"
      app/ tests/` and `grep -R "langfuse.callback" app/ tests/` both return no hits. `ruff check .`
      and `ruff format --check .` pass.
- [ ] REQ-OBS-CALLBACK-001 implemented. `build_callback_handler()` returns a working v3
      `CallbackHandler` when keys present; returns `None` otherwise with a WARNING log line.
- [ ] REQ-OBS-CALLBACK-002 implemented. Webhook flow completes end-to-end when handler is `None`;
      no exception, response delivered.
- [ ] REQ-OBS-DECORATOR-001 implemented. `observe` decorator uses v3 single-path import. Existing
      callers (SPEC-MEMORY-001 memory modules, `app/pipeline/runner.py`) work without source changes.
- [ ] REQ-OBS-TRACE-NODE-001 implemented. All 12 LangGraph nodes have `@observe(name="node.{name}",
      as_type="span")` on entry functions. Integration test asserts all reachable nodes produce one
      span each.
- [ ] REQ-OBS-TRACE-LOOP-001 implemented. Reflexion 2-retry scenario produces 3 `node.search` and 3
      `node.evaluator` sibling spans with `iteration` metadata 0/1/2.
- [ ] REQ-OBS-TRACE-CLARIFY-001 implemented. Two webhooks (turn N: emit clarify card, turn N+1:
      consume clarify callback) produce two traces with the same `session_id`.
- [ ] REQ-OBS-PII-001 implemented. `_hash_for_span` helper exists; static + dynamic tests assert
      raw `chat_id` and `from_user_id` do NOT appear in any span attribute, metadata, or log line
      from observability/webhook/graph modules.
- [ ] REQ-OBS-METADATA-001 implemented. Root trace metadata carries `lang`, `flow`,
      `critique_retry_count`, `chat_id_hash` on every webhook trace.
- [ ] REQ-OBS-COST-001 implemented. Benchmark test shows < 5ms p99 added latency vs Langfuse-disabled
      baseline (post-SPEC build with `LANGFUSE_PUBLIC_KEY=""`). `get_client().flush()` called on
      lifespan exit.
- [ ] REQ-OBS-COST-002 implemented. `LANGFUSE_SELECTIVE_MODE` flag exists; benchmark test produces
      `latency-baseline.json` artifact; rollback procedure documented in `plan.md` and (if
      invoked) recorded in `progress.md`.
- [ ] REQ-OBS-FALLBACK-001 implemented. Empty keys → no-op fallback, startup succeeds. Import
      failure → ERROR log line + no-op fallback, startup succeeds. Unreachable host at startup →
      SDK retry handles it, no impact on hot path.
- [ ] REQ-OBS-FALLBACK-002 implemented. Mid-session host failure absorbed by SDK retry; webhook
      hot path unaffected; zero per-webhook ERROR logs from `app/observability/langfuse.py`; SDK
      auto-resumes flushing on host restoration without app state reset.
- [ ] All existing tests (`pytest -q` baseline before this SPEC, including SPEC-MEMORY-001's
      `tests/test_memory_pg/` if merged) continue to pass under both Langfuse-enabled and
      Langfuse-disabled configurations.
- [ ] **Coverage target (TRUST 5 Tested):** `app/observability/langfuse.py` reports ≥ 85% line
      coverage in `pytest --cov`. New test files in `tests/test_observability/` collectively cover
      every public symbol of the module.
- [ ] `pyproject.toml` v2 pin removed; `uv.lock` regenerated and committed.
- [ ] End-to-end manual test against the dev Telegram bot:
      (a) [REQ-OBS-TRACE-NODE-001, REQ-OBS-METADATA-001] Photo flow → Langfuse UI shows trace with
      root + 7-9 node spans + nested generation spans on `respond`, `vision`, `critique_apply` (if
      hit) — verified visually. Root trace metadata shows `lang` / `flow=image` / `chat_id_hash`.
      (b) [REQ-OBS-TRACE-LOOP-001] Reflexion-retry flow (e.g., overly-narrow filter that triggers
      fast-path broaden) → trace shows 2 `node.search` and 2 `node.evaluator` siblings, each
      `node.evaluator` span carrying `iteration` metadata 0/1.
      (c) [REQ-OBS-TRACE-CLARIFY-001] Clarify card flow → turn N's trace has `node.ask_clarify`,
      turn N+1's trace has `node.apply_clarify`, both share `session_id` in Langfuse UI session view.
      (d) [REQ-OBS-FALLBACK-001] Cold-start with `LANGFUSE_PUBLIC_KEY=""` → bot starts, webhook flow
      succeeds, no Langfuse traffic, one WARN log at startup.
      (e) [REQ-OBS-FALLBACK-001, REQ-OBS-FALLBACK-002] Cold-start with valid keys but
      `LANGFUSE_HOST=http://127.0.0.1:1` → bot starts, webhook flow succeeds, SDK emits its own
      retry warnings but does not block; subsequent webhooks after a host restoration successfully
      resume flushing without app state reset.
- [ ] `ruff check . && ruff format --check .` passes.
- [ ] `pytest -q` passes at the same or higher count vs the pre-SPEC baseline; new test count
      includes ≥ 8 tests in `tests/test_observability/` (langfuse helper, trace shape integration,
      PII static + dynamic, latency benchmark, fallback scenarios).
