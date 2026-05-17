# kiko-ai-server

kiko.ai 패션 추천 AI 서버 — FastAPI 기반 검색/리파인 파이프라인 + Telegram 채널.

`kikoai/app`(Next.js)이 IG 분석 + Vision 처리까지 끝낸 단일 아이템을 받아, **Modal에서 이미지 임베딩 → dev-app Postgres `search_products_v5` RPC (PostgREST nginx shim 경유) → 다양성 캡 → product_id[] 반환**.

Telegram 채널(`@kiko_fashion_ai_bot`): 사용자가 패션 이미지·Pinterest 링크를 DM하면 → webhook → **LangGraph StateGraph** (`app/graphs/`) → 동일 파이프라인 → 채널 카드 응답.

상세 문서:
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 전체 그림 + 토폴로지
- [docs/PATTERNS.md](docs/PATTERNS.md) — 코드 컨벤션
- [docs/features/](docs/features/) — pipeline / search-engine / observability
- [docs/infra/](docs/infra/) — env / deployment / cicd

## 책임 분리 (요약)

| 레이어 | 책임 |
|--------|------|
| dev-app EC2 / `kikoai/app` | R2, Vision(GPT-4o-mini), 세션(Auth.js), UI, v4 폴백. Next.js standalone 컨테이너 |
| **kikoai/ai (이 프로젝트)** | **검색 오케스트레이션, enhance_query, Langfuse trace, Telegram webhook + 채널 어댑터, 대화 이벤트 로그(SPEC-CONVERSATION-LOG-001), 온보딩 카드(SPEC-ONBOARD-CARDS-001)** |
| Apify | Pinterest 핀 스크래퍼 (`epctex/pinterest-scraper`) — SPEC-ONBOARD-CARDS-001 Pinterest bootstrap |
| Telegram Bot API | 채널 transport (메시지 수신/발신). 이 서버에서 블랙박스로 취급 |
| Modal | FashionSigLIP 임베딩 (단건 + 배치) |
| dev-app Postgres + nginx PostgREST shim | pgvector + pgroonga, `search_products_v5` RPC. SPEC-INFRA-MIGRATE-001 P6 이후 자체호스팅 (이전: Supabase) |

> **2026-05-10 컷오버**: Supabase + Vercel pause. dev-app EC2 단독 운영. env 변수는 `DB_URL`/`DB_TOKEN` 으로 리네임 완료 (구 `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`), nginx PostgREST shim (`http://172.31.59.31:3001`) 으로 라우팅.

## 디렉토리

```
app/
├── main.py              # FastAPI 앱 + lifespan + CORS (+ messenger adapter 워밍업)
├── api/                 # 라우터 (recommend, health, webhooks/telegram)
├── agents/              # ReAct 에이전트 루프 + 툴 레지스트리 (SPEC-AGENT-V2-REACT + SPEC-AGENT-V3-REACT, flag-gated)
│   ├── react_loop.py    # ReAct loop 엔진 (iteration cap / infinite-loop guard / token budget / timeout). V3: Gap1 memory injection + Gap2 _maybe_reflexion + Gap3 proactive directive 포함
│   ├── tool_registry.py # 7-tool REGISTRY + validate_args (단일 소스). V3 Gap3 flag ON 시 8-tool (suggest_next_step 추가)
│   ├── llm_client.py    # ChatOpenAI 싱글톤 (bind_tools, LiteLLM proxy 경유)
│   ├── _memory_context.py  # [V3 Gap1 NEW] TasteProfile + 최근 N턴 요약 자동 주입 빌더 (래핑 전용)
│   ├── _reflexion.py       # [V3 Gap2 NEW] evaluator 헬퍼 래핑 — in-loop Reflexion 평가 (래핑 전용)
│   └── tools/           # 툴 래퍼 7개(V2) / 8개(V3 Gap3 ON): analyze_image, search_products, refine_search, update_taste, ask_user_clarification, get_recent_history, respond + suggest_next_step(V3)
├── channels/            # 채널 어댑터 (SPEC-MSG-001): adapter ABC, factory, recommendation port, link_resolver, session, lang, vision (+ vision_prompt, clarify, clarify_values, onboarding_cards, onboarding_values, pinterest_url, _jsonable)
│   └── telegram/        # Telegram 구현 (adapter, webhook 파싱)
├── graphs/              # LangGraph StateGraph (SPEC-AGENT-001): fashion_bot, state, routing
│   └── nodes/           # 18 노드 (V1) / 14 노드 (V2 — agent+intro 신규, deprecated 5개 미등록): ingest, resolve_image, vision, pick_item, ask_clarify, apply_clarify, critique_apply, search, evaluator, send_results, taste_update, respond + onboard_intro, onboard_mood, onboard_color, onboard_fit, onboard_pinterest, pinterest_ingest (SPEC-ONBOARD-CARDS-001) + agent, intro (SPEC-AGENT-V2-REACT). [V3 NEW] _trace.py (logging-only node enter/done/skip 헬퍼)
├── services/            # 비즈니스 서비스 레이어 (SPEC-ARCH-AI-001): embed_service, search_service, diversify_service, database_service
├── infrastructure/      # 인프라 레이어 (SPEC-ARCH-AI-001)
│   ├── repositories/    # SearchRepository (RPC name + param 단일 소스), search_rpc_contract (REQ-AI-006)
│   └── memory/          # 메모리 저장소 — session, session_pg, taste_profile, taste_profile_pg (app/channels/에서 이전)
├── domain/              # 도메인 모델 — SearchResult, Candidate (app/models/과 분리된 도메인 타입)
├── pipeline/            # 검색 파이프라인 thin shim (SPEC-ARCH-AI-001): @observe 래핑 + 테스트 seam 재노출 → 실제 로직은 app/services/ 에 위치
├── providers/           # SupabaseProvider (PostgREST 클라이언트, 논리명 유지), EmbedProvider, LLMProvider, ApifyProvider (SPEC-ONBOARD-CARDS-001)
├── observability/       # Langfuse @observe 래퍼 + build_callback_handler + conversation_log + event_payloads (SPEC-CONVERSATION-LOG-001)
├── models/              # Pydantic request/response
├── core/                # config (env) + di.py (DI 컨테이너 — provide_db_pool / provide_settings / provide_embed_provider, SPEC-ARCH-AI-001 REQ-AI-003) + types.py
```

## 기술 스택

| 영역 | 기술 |
|------|------|
| 프레임워크 | FastAPI + uvicorn |
| 에이전트 오케스트레이션 | **LangGraph** `>=1.1.10` (SPEC-AGENT-001) |
| LLM | LiteLLM proxy 경유 (httpx) + `langchain-openai` (`respond`/`ask_clarify` 노드). **V2 ReAct agent LLM: Bedrock nova-lite (`AGENT_LLM_MODEL`) via LiteLLM** — `drop_params: true` 로 `tool_choice` 제거 (Bedrock 호환) |
| 임베딩 | Modal HTTP endpoint (FashionSigLIP) |
| 벡터 DB | **dev-app Postgres 16 + pgvector + pgroonga** (PostgREST nginx shim, Qdrant 미사용) |
| Observability | **Langfuse self-host** (`build_callback_handler` — langfuse v2+langchain 비호환으로 현재 None 폴백) |
| 스키마 | Pydantic v2 |
| HTTP | httpx (async) |
| 패키지 | uv |
| 린트 | ruff |

## 개발 명령어

```bash
uv sync                                              # 의존성 설치
uv run uvicorn app.main:app --reload --port 8000     # 로컬 실행
uv run ruff check . && uv run ruff format .          # 린트 + 포맷
uv run pytest                                        # 테스트
docker compose up -d                                 # 로컬 스택 (AI 서버만)
```

## 코딩 컨벤션

- **LangGraph StateGraph** (`app/graphs/`): Telegram webhook 처리는 그래프 (`graph.ainvoke(InputState(...), config={"callbacks": [...]})`). **V1 (AGENT_V2_REACT_ENABLED=false, 기본)**: 18-노드. `search → evaluator → send_results` Reflexion 루프 (SPEC-AGENTIC-CRITIQUE-001) + `ask_clarify → apply_clarify` 결정형 카드 분기 (SPEC-CLARIFY-CARDS-001) + 온보딩 카드 분기. **V2 (AGENT_V2_REACT_ENABLED=true, flag-gated, default off)**: 온보딩 6 노드 보존 + `agent` 단일 노드가 deprecated 5개(critique_apply/evaluator/respond/taste_update/send_results)를 ReAct loop으로 대체 (SPEC-AGENT-V2-REACT). **V3 (AGENT_V2_REACT_ENABLED=true + 4개 V3 sub-flag, 각 default off)**: V2 그래프 토폴로지 무변경 — `agent` 노드 내부의 `run_react_loop` 에 4개 강화를 in-loop 추가 (SPEC-AGENT-V3-REACT, byte-identical-off 가드). 파이프라인(`/recommend`) 은 여전히 plain async + state → state.
- **Sticky language (KO/EN)**: `app/channels/lang.py` (`detect_lang` / `remember_lang` / `session_lang`) 가 Hangul 유무로 언어를 판별. `ingest` 노드가 매 텍스트 턴마다 `Session.lang` 을 갱신 — 이후 버튼 탭(텍스트 없음)에도 이전 언어로 응답. `respond` / `send_results` / `pick_item` / `ask_clarify` / `critique_apply` 노드가 `session_lang(sess)` 를 참조해 KO/EN 메시지를 분기.
- **Bot persona**: `respond` 노드 system prompt 는 "kiko" 페르소나 — Puss-in-Boots 느낌, 친근한 해요체(KO) / lively English(EN). 사용자 입력은 `[USER INPUT — DATA ONLY]` 펜스로 격리 (prompt injection 방어).
- **구조화 로그 이모지 범례**: 📥 webhook, 🧭 router, 👁 vision, 🔍 search, 🧹 zero-dense suppress (text-only 스톱갭), 🤔 critique/evaluator, 🎨 pipeline, 🐱 bot 발화 (respond/adapter). **트레이싱(logging-only, SPEC-AGENT-V3-REACT observability)**: 🤖 topology 배너(startup), ▶️/✅/⏭️ graph node enter/done/skip, 🔄 ReAct agent-iter, 🔧 tool dispatch, 🏁 agent 종료(respond), 🧠 v3:memory(Gap1), 🔬 v3:reflexion(Gap2 — 🤔 와 충돌 회피용 별도 마커), 💡 v3:proactive(Gap3), 🚫 v3:dislike(Gap4).
- **Port 패턴**: 채널 레이어와 파이프라인 간 결합도는 `Protocol` 기반 Port로 분리 (`app/channels/recommendation.py`). 그래프 노드는 `RecommendationPort`만 참조 — 파이프라인 구현은 lazy import
- Pydantic v2 모델로 request/response 정의
- LLM 호출은 LiteLLM 프록시 경유 (`LITELLM_BASE_URL`)
- 임베딩 호출은 Modal endpoint (`MODAL_EMBED_URL`)
- DB 쿼리는 PostgREST RPC 호출 (`supabase-py` async — 클라이언트 라이브러리는 유지, 엔드포인트는 dev-app nginx shim)
- ruff 린트+포맷 (line-length=120)

## 핵심 파일

| 파일 | 설명 |
|------|------|
| `app/main.py` | FastAPI 엔트리포인트 + lifespan (DB 클라이언트 워밍업 + messenger adapter + setWebhook) |
| `app/api/recommend.py` | `POST /recommend` (X-Internal-Token 인증) |
| `app/api/health.py` | `/health` (liveness, no auth) + `/health/ready` (인증 + messenger 상태) |
| `app/api/webhooks/telegram.py` | `POST /webhooks/telegram` (X-Telegram-Bot-Api-Secret-Token 인증) |
| `app/channels/adapter.py` | `MessengerAdapter` ABC |
| `app/channels/factory.py` | `MESSENGER_BACKEND` 기반 어댑터 팩토리 |
| `app/channels/recommendation.py` | `RecommendationPort` Protocol + `ChannelRecommendationRequest/Result` DTO + `PipelineRecommendationPort` 구현 (채널-파이프라인 결합도 분리) |
| `app/agents/react_loop.py` | ReAct loop 엔진 — iteration cap / infinite-loop guard / token budget / per-tool timeout / tool_call event emit (SPEC-AGENT-V2-REACT). V3: Gap1 memory injection + Gap2 `_maybe_reflexion` (잔여-budget asyncio.wait_for 강제 취소) + Gap3 `_PROACTIVE_DIRECTIVE` (SPEC-AGENT-V3-REACT) |
| `app/agents/tool_registry.py` | 7-tool(V2)/8-tool(V3 Gap3 ON) REGISTRY + TypedDict args/result schema + `validate_args` (단일 소스). V3: `SuggestNextStepArgs/Result` + flag-aware 모듈-로드 분기 (SPEC-AGENT-V3-REACT) |
| `app/agents/llm_client.py` | ChatOpenAI 싱글톤 (bind_tools, LiteLLM proxy 경유). `AGENT_LLM_MODEL` 미설정 시 fail-closed |
| `app/agents/_memory_context.py` | [V3 Gap1 NEW] `build_memory_context(state, sess, ctx) -> str` — TasteProfile + 최근 N턴(기본 5) 요약 자동 주입, char-cap(`AGENT_V3_MEMORY_MAX_TOKENS`*4), `[MEMORY CONTEXT — SYSTEM DERIVED]` 펜스 (SPEC-AGENT-V3-REACT) |
| `app/agents/_reflexion.py` | [V3 Gap2 NEW] `evaluate_search_quality(...) -> dict` — `evaluator._call_llm`/`_build_fastpath_delta` 래핑, fail-open(score=1.0), 빈결과 fastpath. 자체 LLM 프롬프트 신규 정의 없음 (SPEC-AGENT-V3-REACT) |
| `app/agents/tools/suggest_next_step.py` | [V3 Gap3 NEW] 8번째 tool — `_adapter_ctx.get_adapter()` + `send_text_with_buttons` 재사용 (≤80 LOC, 새 카드 렌더 없음, `terminates_loop=False`) (SPEC-AGENT-V3-REACT) |
| `app/graphs/nodes/agent.py` | LangGraph `agent` 노드 — `run_react_loop` 래핑, state delta 반환 (SPEC-AGENT-V2-REACT) |
| `app/graphs/fashion_bot.py` | LangGraph StateGraph 빌드 + 모듈 수준 컴파일 캐시 + `build_callback_handler` (SPEC-AGENT-001). `AGENT_V2_REACT_ENABLED` 플래그로 V1/V2 토폴로지 분기 |
| `app/graphs/state.py` | `InputState`, `WorkingState`, `OutputState` Pydantic v2 모델. V2: `agent_iterations`, `tool_call_history`, `agent_status` 3 필드 추가 |
| `app/graphs/routing.py` | 조건부 엣지 함수 (after_ingest, after_resolve_image, after_vision, after_pick, after_critique, after_search + 온보딩 분기 포함). V2 신규: `first_touch_intro_required` (ONBOARDING_CARDS_ENABLED=false + onboarded_at IS NULL 시 intro 진입 판단) |
| `app/graphs/nodes/intro.py` | 첫 방문 서비스 소개 메시지 — `ONBOARDING_CARDS_ENABLED=false` 시 신규 사용자(`onboarded_at IS NULL`)에게 1회성 상세 안내 발송, `onboarded_at` 기록 후 턴 종료. KO/EN 분기 (SPEC-AGENT-V2-REACT) |
| `app/graphs/nodes/onboard_intro.py` | 온보딩 인트로 카드 — `/start` + `onboarded_at IS NULL` 시 진입, 기본 언어 KO 설정 (SPEC-ONBOARD-CARDS-001) |
| `app/graphs/nodes/onboard_mood.py` | 온보딩 Stage 1 — 무드 카드 (4 axes) |
| `app/graphs/nodes/onboard_color.py` | 온보딩 Stage 2 — 컬러 카드 |
| `app/graphs/nodes/onboard_fit.py` | 온보딩 Stage 3 — 핏 카드 + 완료 시 `seed_from_onboarding` → TasteProfile 시드 |
| `app/graphs/nodes/onboard_pinterest.py` | 온보딩 Stage 4 (선택) — Pinterest 보드 URL 요청 카드. `PINTEREST_BOOTSTRAP_ENABLED` + `APIFY_TOKEN` 분기 |
| `app/graphs/nodes/pinterest_ingest.py` | Pinterest URL 수신 → Apify 스크래핑 (board/profile) or link_resolver (mode C) → Vision batch → TasteProfile reinforce (SPEC-ONBOARD-CARDS-001) |
| `app/graphs/nodes/_onboard_helpers.py` | 온보딩 내부 헬퍼 — `seed_from_onboarding`, 완료 메시지 빌더, stage 전환 등 |
| `app/graphs/nodes/_onboard_stage.py` | 온보딩 stage enum + 전환 테이블 |
| `app/graphs/nodes/_pinterest_helpers.py` | Pinterest 핀 배치 처리 — Vision v2 schema batch + `TasteProfile.reinforce_liked_*` 머지 |
| `app/graphs/nodes/respond.py` | 자연어 reply 생성 — `ChatOpenAI` (`RESPONSE_MODEL`, `RESPONSE_MAX_TOKENS`, `RESPONSE_TIMEOUT_MS`). "kiko" 페르소나 system prompt + KO/EN 분기 fallback. 12 flows (`_Flow` enum, `STALE_CRITIQUE` 포함). `critique_exhausted` 시 톤 완화 |
| `app/graphs/nodes/ask_clarify.py` | weak-vision 시 인라인 키보드 카드 생성 (SPEC-CLARIFY-CARDS-001, 6 axes, LLM 호출 없음) |
| `app/graphs/nodes/apply_clarify.py` | `clarify:*` callback 소비 → `session.boost_keywords` 누적 (SPEC-CLARIFY-CARDS-001) |
| `app/graphs/nodes/evaluator.py` | search 결과 평가 → 빈 결과 fast-path (필터 drop) / LLM 평가 → `CritiqueDelta` 재시도 (SPEC-AGENTIC-CRITIQUE-001, 최대 2회 + 4 안전 가드) |
| `app/channels/link_resolver.py` | Pinterest / pin.it og:image URL 해석 |
| `app/channels/lang.py` | 언어 감지 헬퍼 — `detect_lang` / `remember_lang` / `session_lang`. Hangul 유무 기준 KO/EN 판별, `Session.lang` sticky 갱신 |
| `app/infrastructure/memory/session.py` | `SessionStore` Protocol + `InMemorySessionStore` 구현체. `set_store_factory/set_store/reset_store` 주입 지점 포함. `Session.lang: str = "en"` (sticky 언어 필드). 구 경로: `app/channels/session.py` (SPEC-ARCH-AI-001 이전) |
| `app/channels/vision.py` | LiteLLM 경유 Vision 패션 아이템 추출 — v2 schema (SPEC-VISION-UNIFY-001): `styleNode`/`sensitivityTags`/`mood`/`palette`/`style`/`items[]` (subcategory/fit/colorFamily/searchQuery/searchQueryKo). flag: `VISION_SCHEMA_V2` |
| `app/channels/vision_prompt.py` | Vision v2 schema 프롬프트 + JSON 스키마 정의 (kikoai/app `analyze.ts` 동치) |
| `app/channels/clarify.py` | clarify 카드 빌더 (6 axes: category_pick / formality / fit / occasion / subcategory_disambiguation / generic_fallback) |
| `app/channels/clarify_values.py` | clarify 카드 axis별 옵션 값 + 한글 라벨 매핑 |
| `app/channels/onboarding_cards.py` | 온보딩 카드 빌더 (4 axes: mood/color/fit/pinterest — SPEC-ONBOARD-CARDS-001) |
| `app/channels/onboarding_values.py` | 온보딩 카드 axis별 옵션 값 + KO/EN 라벨 + keywords_to_boost 매핑 |
| `app/channels/pinterest_url.py` | Pinterest URL 파싱/검증 — board/profile/pin 모드 판별, SSRF allowlist 적용 |
| `app/channels/_jsonable.py` | 5-step JSON-serializable cascade 헬퍼 (session_pg / taste_profile_pg / conversation_log 공용 — SPEC-MEMORY-001 패턴 추출) |
| `app/channels/telegram/adapter.py` | TelegramAdapter (sendMessage / sendPhoto / InlineKeyboard / edit_inline_keyboard) |
| `app/channels/telegram/webhook.py` | Telegram Update 파싱 |
| `app/core/auth.py` | `verify_internal_token` FastAPI dependency |
| `app/pipeline/state.py` | PipelineState 정의 |
| `app/pipeline/embed.py` | thin @observe shim — 실제 로직은 `app/services/embed_service.py`에 위치 (SPEC-ARCH-AI-001) |
| `app/pipeline/enhance_query.py` | LLM 기반 sparse 쿼리 정제 (SPEC-PIPELINE-001, feature flag 기본 off) |
| `app/pipeline/search.py` | thin @observe shim — 실제 로직은 `app/services/search_service.py` + `app/infrastructure/repositories/search_repository.py`에 위치. 테스트 monkeypatch seam(`SupabaseProvider.rpc`) 재노출 (SPEC-ARCH-AI-001) |
| `app/pipeline/diversify.py` | thin @observe shim — 실제 로직은 `app/services/diversify_service.py`에 위치 (SPEC-ARCH-AI-001) |
| `app/pipeline/runner.py` | 파이프라인 조립 + `@observe` |
| `app/services/search_service.py` | 검색 오케스트레이션 + 3-tier query_text 선택. `RpcContractError` 캐치 → 구조화 ERROR 로그 + fail-open 빈 결과 (REQ-AI-006, SPEC-ARCH-AI-001) |
| `app/services/diversify_service.py` | 브랜드/플랫폼 캡 + tolerance 산술. banker's rounding 포함 (`int(round(10 + t*10))`) (SPEC-ARCH-AI-001) |
| `app/services/embed_service.py` | Modal /embed 래핑 (SPEC-ARCH-AI-001) |
| `app/services/database_service.py` | `SupabaseProvider` pass-through 래퍼 (SPEC-ARCH-AI-001) |
| `app/infrastructure/repositories/search_repository.py` | `SearchRepository` — `_RPC_NAME = "search_products_v5"` (단일 소스) + `build_params` + `search`. `embedding_to_pgvector` 공동 위치 (SPEC-ARCH-AI-001 REQ-AI-002) |
| `app/infrastructure/repositories/search_rpc_contract.py` | `SearchRpcRowContract` Pydantic 모델 + `RpcContractError` + `validate_rpc_rows`. 드리프트 시 구조화 에러 (REQ-AI-006). 허용: id absent → 에러, score/brand absent → 허용, extra 컬럼 허용 |
| `app/infrastructure/memory/session.py` | (구 `app/channels/session.py`) `SessionStore` Protocol + `InMemorySessionStore` (SPEC-ARCH-AI-001) |
| `app/infrastructure/memory/session_pg.py` | (구 `app/channels/session_pg.py`) Postgres 기반 세션 저장소 (SPEC-ARCH-AI-001) |
| `app/infrastructure/memory/taste_profile.py` | (구 `app/channels/taste_profile.py`) TasteProfile 도메인 모델 (SPEC-ARCH-AI-001) |
| `app/infrastructure/memory/taste_profile_pg.py` | (구 `app/channels/taste_profile_pg.py`) Postgres 기반 취향 프로파일 저장소 (SPEC-ARCH-AI-001) |
| `app/core/di.py` | DI 컨테이너 — `provide_db_pool` / `provide_settings` / `provide_embed_provider`. `db_pool` 모듈 글로벌 상태 위임 어댑터로 유지 (byte-identical, REQ-AI-003) |
| `app/core/types.py` | 공유 타입 — `ProductRow` 등 (SPEC-ARCH-AI-001) |
| `app/domain/search.py` | 도메인 타입 — `SearchResult`, `Candidate` (app/models/ DTO 와 분리) (SPEC-ARCH-AI-001) |
| `app/providers/database.py` | SupabaseProvider — PostgREST 클라이언트 (논리명 유지, async, lifespan 워밍업) |
| `app/providers/embedding.py` | Modal HTTP + 응답 스키마 검증 |
| `app/providers/llm.py` | LiteLLM HTTP |
| `app/providers/apify.py` | Apify Pinterest 스크래퍼 — `run_pinterest_scrape(url, mode, max_items, timeout_s)`. `ApifyTimeoutError` 전용 예외. board/profile/pin 3-mode 지원. httpx 직접 사용 (SDK 미사용, SPEC-ONBOARD-CARDS-001) |
| `app/observability/langfuse.py` | `@observe` (no-op fallback) + langfuse env 자동 주입 + `current_langfuse_trace_id()` export |
| `app/observability/conversation_log.py` | 대화 이벤트 로거 — `log_event()` (async, never raises) + `emit()` (fire-and-forget asyncio.create_task) + `_truncate()` payload cap. `MEMORY_BACKEND_IS_POSTGRES` 가드. 모든 graph 노드 + webhook intake 에서 호출 (SPEC-CONVERSATION-LOG-001) |
| `app/observability/event_payloads.py` | 20개 이벤트 타입 TypedDict 정의 (`user_text`, `user_photo`, `intent_routed`, `vision_done`, `search_done`, `diversify_done`, `card_sent`, `card_clicked`, `bot_text`, `taste_update`, `node_error`, `tool_call` 등 — SPEC-CONVERSATION-LOG-001 + SPEC-AGENT-V2-REACT) |
| `app/models/request.py` | RecommendRequest (alias + image_url SSRF 가드) |
| `app/models/response.py` | RecommendResponse (serialization_alias) |

## 검색 책임 경계 (B 옵션)

```
[Postgres RPC] dense(HNSW) + sparse(pgroonga) + RRF → top-50
       ↓
[Python] 다양성 캡(브랜드/플랫폼) + tolerance + 최종 정렬 → top-15
```

## 환경 변수

`.env.example` 참조. 키는 `.env`에 (POC 단계 — 운영 시 Parameter Store 전환 예정).

주요 feature flag (상세는 `docs/infra/env.md`):
- `VISION_SCHEMA_V2` (기본 `true`) — Vision 풍부 스키마 (SPEC-VISION-UNIFY-001)
- `SELF_CRITIQUE_ENABLED` (기본 `true`) + `SELF_CRITIQUE_MAX_ITERATIONS` / `SELF_CRITIQUE_THRESHOLD` / `SELF_CRITIQUE_TIMEOUT_S` / `SELF_CRITIQUE_FASTPATH_DROP_FILTERS` + `EVALUATOR_MODEL` / `EVALUATOR_MAX_TOKENS` / `EVALUATOR_TEMPERATURE` / `EVALUATOR_TIMEOUT_S` (SPEC-AGENTIC-CRITIQUE-001)
- `CLARIFY_CARDS_ENABLED` (기본 `true`) + `CLARIFY_MAX_BUTTONS` (SPEC-CLARIFY-CARDS-001)
- `RESPONSE_SPLIT_ENABLED` (기본 `true`) + `RESPONSE_SPLIT_DELAY_MS` / `RESPONSE_SPLIT_MIN_CHARS` — 문장 단위 분할 발화 (noscroll benchmark P0)
- `ONBOARDING_CARDS_ENABLED` (기본 `true`) + `PINTEREST_BOOTSTRAP_ENABLED` (기본 `true`) + `APIFY_TOKEN` / `APIFY_PINTEREST_ACTOR` / `APIFY_PINTEREST_MAX_ITEMS` / `APIFY_PINTEREST_CONCURRENCY` / `ONBOARDING_SEED_MAX_WEIGHT` (SPEC-ONBOARD-CARDS-001)
- `AGENT_V2_REACT_ENABLED` (기본 `false`, **운영 기본 off**) + `AGENT_LLM_MODEL` (미설정 시 fail-closed — effective gate, 실제 운영값: `nova-lite` via LiteLLM) + `AGENT_MAX_ITERATIONS` / `AGENT_TURN_TOKEN_BUDGET` / `AGENT_TOOL_TIMEOUT_S` / `AGENT_LLM_TIMEOUT_S` / `AGENT_LLM_MAX_RETRIES` / `AGENT_TOOL_MAX_RETRIES` / `AGENT_RESPOND_TIMEOUT_S` (SPEC-AGENT-V2-REACT, flag-gated, default off in prod)
- **[V3 — SPEC-AGENT-V3-REACT]** 마스터 `AGENT_V2_REACT_ENABLED=true` 시에만 유효한 4개 sub-flag (모두 **운영 기본 `false`**, all-off 시 V2 byte-identical):
  - `AGENT_V3_MEMORY_INJECTION_ENABLED` (기본 `false`) — Gap1: TasteProfile + 최근 5턴 요약을 system context에 자동 주입
  - `AGENT_V3_REFLEXION_ENABLED` (기본 `false`) — Gap2: search/refine 결과를 기존 evaluator 헬퍼로 평가 → quality delta 첨부 → LLM 자율 refine (SPEC-AGENT-V2-REACT OQ-7 resolution)
  - `AGENT_V3_PROACTIVE_ENABLED` (기본 `false`) — Gap3: 8번째 tool `suggest_next_step` 등록 + system prompt 능동성 지침
  - `AGENT_V3_DISLIKE_MEMORY_ENABLED` (기본 `false`) — Gap4: 크로스스레드 dislike timestamp + 자동 디스카운트 (Alembic 0005 마이그레이션 선행 필수)
  - `AGENT_V3_MEMORY_MAX_TOKENS` (기본 `1500`) — Gap1 메모리 주입 페이로드 token cap (char 근사: *4)

## 관련 프로젝트

| 프로젝트 | 경로 | 역할 |
|----------|------|------|
| kikoai/app | `/Users/hansangho/Desktop/kikoai/app` | Next.js 모놀리스 (caller + v4 폴백) |
| aws-infra | `/Users/hansangho/Desktop/aws-infra/kiko-ai-servers/portal-ai/` | EC2 docker-compose + Langfuse + Modal 인프라 |

## 인증 구조

AI 서버는 stateless. 인증 없음.
`kikoai/app`이 세션 + Auth.js v5 (Credentials Provider + bcrypt) 담당, AI 서버에 request body로 전달.
