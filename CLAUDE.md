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
├── channels/            # 채널 어댑터 (SPEC-MSG-001): adapter ABC, factory, recommendation port, link_resolver, session, lang, vision (+ vision_prompt, clarify, clarify_values, onboarding_cards, onboarding_values, pinterest_url, _jsonable)
│   └── telegram/        # Telegram 구현 (adapter, webhook 파싱)
├── graphs/              # LangGraph StateGraph (SPEC-AGENT-001): fashion_bot, state, routing
│   └── nodes/           # 18 노드: ingest, resolve_image, vision, pick_item, ask_clarify, apply_clarify, critique_apply, search, evaluator, send_results, taste_update, respond + onboard_intro, onboard_mood, onboard_color, onboard_fit, onboard_pinterest, pinterest_ingest (SPEC-ONBOARD-CARDS-001)
├── pipeline/            # 검색 파이프라인 (embed → search → diversify)
├── providers/           # SupabaseProvider (PostgREST 클라이언트, 논리명 유지), EmbedProvider, LLMProvider, ApifyProvider (SPEC-ONBOARD-CARDS-001)
├── observability/       # Langfuse @observe 래퍼 + build_callback_handler + conversation_log + event_payloads (SPEC-CONVERSATION-LOG-001)
├── models/              # Pydantic request/response
└── core/                # config (env)
```

## 기술 스택

| 영역 | 기술 |
|------|------|
| 프레임워크 | FastAPI + uvicorn |
| 에이전트 오케스트레이션 | **LangGraph** `>=1.1.10` (SPEC-AGENT-001) |
| LLM | LiteLLM proxy 경유 (httpx) + `langchain-openai` (`respond`/`ask_clarify` 노드) |
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

- **LangGraph StateGraph** (`app/graphs/`): Telegram webhook 처리는 18-노드 그래프 (`graph.ainvoke(InputState(...), config={"callbacks": [...]})`). `search → evaluator → send_results` Reflexion 루프 (SPEC-AGENTIC-CRITIQUE-001) + `ask_clarify → apply_clarify` 결정형 카드 분기 (SPEC-CLARIFY-CARDS-001) + `onboard_intro → onboard_mood → onboard_color → onboard_fit → onboard_pinterest → pinterest_ingest` 3-stage 온보딩 카드 분기 (SPEC-ONBOARD-CARDS-001). 파이프라인(`/recommend`) 은 여전히 plain async + state → state.
- **Sticky language (KO/EN)**: `app/channels/lang.py` (`detect_lang` / `remember_lang` / `session_lang`) 가 Hangul 유무로 언어를 판별. `ingest` 노드가 매 텍스트 턴마다 `Session.lang` 을 갱신 — 이후 버튼 탭(텍스트 없음)에도 이전 언어로 응답. `respond` / `send_results` / `pick_item` / `ask_clarify` / `critique_apply` 노드가 `session_lang(sess)` 를 참조해 KO/EN 메시지를 분기.
- **Bot persona**: `respond` 노드 system prompt 는 "kiko" 페르소나 — Puss-in-Boots 느낌, 친근한 해요체(KO) / lively English(EN). 사용자 입력은 `[USER INPUT — DATA ONLY]` 펜스로 격리 (prompt injection 방어).
- **구조화 로그 이모지 범례**: 📥 webhook, 🧭 router, 👁 vision, 🔍 search, 🤔 critique/evaluator, 🎨 pipeline, 🐱 bot 발화 (respond/adapter).
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
| `app/graphs/fashion_bot.py` | LangGraph StateGraph 빌드 + 모듈 수준 컴파일 캐시 + `build_callback_handler` (SPEC-AGENT-001) |
| `app/graphs/state.py` | `InputState`, `WorkingState`, `OutputState` Pydantic v2 모델 |
| `app/graphs/routing.py` | 조건부 엣지 함수 (after_ingest, after_resolve_image, after_vision, after_pick, after_critique, after_search + 온보딩 분기 포함) |
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
| `app/channels/session.py` | `SessionStore` Protocol + `InMemorySessionStore` 구현체. `set_store_factory/set_store/reset_store` 주입 지점 포함. `Session.lang: str = "en"` (sticky 언어 필드) |
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
| `app/pipeline/embed.py` | Modal /embed 호출 |
| `app/pipeline/enhance_query.py` | LLM 기반 sparse 쿼리 정제 (SPEC-PIPELINE-001, feature flag 기본 off) |
| `app/pipeline/search.py` | `search_products_v5` RPC (PostgREST 경유) |
| `app/pipeline/diversify.py` | 다양성 캡 + tolerance |
| `app/pipeline/runner.py` | 파이프라인 조립 + `@observe` |
| `app/providers/database.py` | SupabaseProvider — PostgREST 클라이언트 (논리명 유지, async, lifespan 워밍업) |
| `app/providers/embedding.py` | Modal HTTP + 응답 스키마 검증 |
| `app/providers/llm.py` | LiteLLM HTTP |
| `app/providers/apify.py` | Apify Pinterest 스크래퍼 — `run_pinterest_scrape(url, mode, max_items, timeout_s)`. `ApifyTimeoutError` 전용 예외. board/profile/pin 3-mode 지원. httpx 직접 사용 (SDK 미사용, SPEC-ONBOARD-CARDS-001) |
| `app/observability/langfuse.py` | `@observe` (no-op fallback) + langfuse env 자동 주입 + `current_langfuse_trace_id()` export |
| `app/observability/conversation_log.py` | 대화 이벤트 로거 — `log_event()` (async, never raises) + `emit()` (fire-and-forget asyncio.create_task) + `_truncate()` payload cap. `MEMORY_BACKEND_IS_POSTGRES` 가드. 모든 graph 노드 + webhook intake 에서 호출 (SPEC-CONVERSATION-LOG-001) |
| `app/observability/event_payloads.py` | 19개 이벤트 타입 TypedDict 정의 (`user_text`, `user_photo`, `intent_routed`, `vision_done`, `search_done`, `diversify_done`, `card_sent`, `card_clicked`, `bot_text`, `taste_update`, `node_error` 등 — SPEC-CONVERSATION-LOG-001) |
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

## 관련 프로젝트

| 프로젝트 | 경로 | 역할 |
|----------|------|------|
| kikoai/app | `/Users/hansangho/Desktop/kikoai/app` | Next.js 모놀리스 (caller + v4 폴백) |
| aws-infra | `/Users/hansangho/Desktop/aws-infra/kiko-ai-servers/portal-ai/` | EC2 docker-compose + Langfuse + Modal 인프라 |

## 인증 구조

AI 서버는 stateless. 인증 없음.
`kikoai/app`이 세션 + Auth.js v5 (Credentials Provider + bcrypt) 담당, AI 서버에 request body로 전달.
