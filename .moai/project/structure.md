# structure.md — kiko.ai AI 서버 디렉토리 구조

kiko.ai AI 서버의 디렉토리 트리, 각 모듈 책임, 진입점, 아키텍처 패턴을 설명한다.

---

## 루트 트리

```
kiko-ai-server/
├── app/                        # FastAPI 애플리케이션 본체
│   ├── main.py                 # 앱 팩토리, lifespan, CORS, messenger adapter 워밍업
│   ├── api/
│   │   ├── __init__.py         # APIRouter 조립 (include_router)
│   │   ├── recommend.py        # POST /recommend (X-Internal-Token 인증)
│   │   ├── health.py           # GET /health, GET /health/ready
│   │   └── webhooks/
│   │       └── apple_notifications.py  # POST /webhooks/apple (App Store Server Notifications)
│   ├── channels/               # 채널 어댑터 레이어 (SPEC-MSG-001)
│   │   ├── adapter.py          # MessengerAdapter ABC
│   │   ├── factory.py          # MESSENGER_BACKEND 기반 어댑터 팩토리
│   │   ├── recommendation.py   # RecommendationPort Protocol + ChannelRecommendationRequest/Result DTO
│   │   ├── vision.py           # GPT-4o-mini Vision (rich VisionResult 반환)
│   │   ├── vision_prompt.py    # ANALYZE_SYSTEM_PROMPT / ANALYZE_USER_PROMPT 상수 모듈
│   │   ├── clarify.py          # ClarifyAxis, ClarifyDelta, parse_callback, pick_clarify_axis
│   │   ├── clarify_values.py   # 축별 enum 값 + 매핑 표
│   │   ├── critique.py         # 사용자 주도 crit:* 콜백 헬퍼
│   │   ├── link_resolver.py    # Pinterest / pin.it og:image URL 해석
│   │   ├── router.py           # text routing LLM 헬퍼
│   │   ├── session.py          # SessionStore Protocol + InMemorySessionStore
│   │   ├── taste_profile.py    # 장기 취향 프로파일 업데이트
│   ├── graphs/                 # LangGraph StateGraph (SPEC-AGENT-001)
│   │   ├── fashion_bot.py      # StateGraph 빌드 + 모듈 수준 컴파일 캐시
│   │   ├── state.py            # InputState, WorkingState, OutputState Pydantic v2 모델
│   │   ├── routing.py          # 6개 + 1개(after_evaluator) 조건부 엣지 함수
│   │   └── nodes/              # 12 노드
│   │       ├── ingest.py
│   │       ├── resolve_image.py
│   │       ├── vision.py
│   │       ├── pick_item.py
│   │       ├── ask_clarify.py      # 인라인 키보드 카드 전송 (SPEC-CLARIFY-CARDS-001)
│   │       ├── apply_clarify.py    # ClarifyDelta WorkingState 보강 (NEW)
│   │       ├── critique_apply.py   # 사용자 crit:* 콜백 처리
│   │       ├── evaluator.py        # Reflexion 자기-비평 노드 (NEW, SPEC-AGENTIC-CRITIQUE-001)
│   │       ├── evaluator_prompt.py # 평가자 프롬프트 상수
│   │       ├── search.py
│   │       ├── send_results.py
│   │       ├── taste_update.py
│   │       └── respond.py
│   ├── core/
│   │   ├── config.py           # pydantic-settings (환경변수 로딩)
│   │   └── auth.py             # verify_internal_token dependency
│   ├── models/
│   │   ├── request.py          # RecommendRequest (alias + SSRF guard)
│   │   └── response.py         # RecommendResponse (serialization_alias)
│   ├── pipeline/
│   │   ├── state.py            # PipelineState dataclass (enhanced_query_* 필드 포함)
│   │   ├── embed.py            # Step 1a: Modal /embed 호출
│   │   ├── enhance_query.py    # Step 1b: LLM 쿼리 정제 (병렬, feature flag, raw 폴백)
│   │   ├── search.py           # Step 2: Supabase RPC 호출 (refined query 우선)
│   │   ├── diversify.py        # Step 3: 다양성 캡 + tolerance
│   │   └── runner.py           # 파이프라인 조립 + @observe
│   ├── providers/
│   │   ├── database.py         # SupabaseProvider (singleton, async)
│   │   ├── embedding.py        # EmbedProvider (Modal HTTP)
│   │   └── llm.py              # LLMProvider (LiteLLM HTTP)
│   └── observability/
│       └── langfuse.py         # @observe v2/v3 호환, no-op fallback
├── docs/                       # 아키텍처 + 운영 문서
│   ├── ARCHITECTURE.md
│   ├── PATTERNS.md
│   ├── features/               # pipeline, search-engine, observability
│   └── infra/                  # env, deployment, cicd
├── tests/                      # pytest-asyncio 테스트 (263개)
│   ├── conftest.py             # httpx AsyncClient fixture
│   ├── test_health.py
│   ├── test_config.py
│   ├── test_enhance_query.py
│   ├── test_pipeline_with_enhance.py
│   ├── test_graph_flows.py     # LangGraph 전체 흐름 테스트
│   ├── test_graph_safety.py    # 자기-비평 루프 안전 가드 테스트
│   ├── test_recommendation_port.py
│   ├── test_vision_schema_parity.py
│   ├── channels/               # 채널 레이어 단위 테스트
│   └── test_graph_nodes/       # 노드별 단위 테스트
├── scripts/
│   └── embed_batch_local.py    # 로컬 배치 임베딩 (운영 이미지 미포함)
├── .moai/                      # MoAI 프로젝트 컨텍스트
├── pyproject.toml              # 의존성 + ruff + pytest 설정
├── uv.lock                     # 결정론적 잠금 파일
├── Dockerfile                  # multi-stage uv 빌드
├── docker-compose.yml          # 로컬 스택 (AI 서버)
├── litellm-config.yaml         # LiteLLM proxy 설정
├── CLAUDE.md                   # 프로젝트 컨텍스트 (AI 에이전트용)
└── README.md
```

---

## 디렉토리별 책임

| 디렉토리 | 책임 |
|---------|------|
| `app/` | FastAPI 애플리케이션 전체 |
| `app/api/` | HTTP 라우터. chat(SSE, 앱 JWT) + auth + recommend(인증 있음) + health(liveness/readiness) |
| `app/channels/` | 채널 어댑터 레이어(SPEC-MSG-001). MessengerAdapter ABC, StreamingAdapter(SSE), Vision, Clarify, Session, LinkResolver |
| `app/graphs/` | LangGraph StateGraph (SPEC-AGENT-001). fashion_bot + state + routing + 12 nodes |
| `app/core/` | 환경변수 로딩(`config.py`) + 인증 dependency(`auth.py`) |
| `app/models/` | Pydantic v2 request/response 스키마. camelCase ↔ snake_case alias |
| `app/pipeline/` | 검색 파이프라인 state machine. (embed ‖ enhance_query) → search → diversify |
| `app/providers/` | 외부 서비스 클라이언트 singleton. Supabase / Modal / LiteLLM |
| `app/observability/` | Langfuse `@observe` 데코레이터 래퍼. SDK 버전 호환 + no-op fallback |
| `docs/` | 아키텍처, 코드 패턴, 운영 인프라 문서 |
| `tests/` | 단위 + 통합 테스트 (263개). pytest-asyncio `mode=auto` |
| `scripts/` | 운영 이외 유틸. 로컬 배치 임베딩 스크립트 |

---

## 진입점

### 서버 진입점

```
app.main:app
```

`app/main.py` 가 FastAPI 인스턴스를 생성하고 다음을 설정한다:

- **lifespan**: startup 시 `SupabaseProvider.get_client()` 워밍업 + 메모리/Redis/캐시 워밍업 → shutdown 시 모든 Provider close
- **CORS**: `allow_origins=["*"]`, `allow_credentials=False` (stateless)
- **default_response_class**: `ORJSONResponse` (orjson 직렬화)

### 라우터 구조

```
app/api/__init__.py
  ├── /recommend          (POST)   → recommend.py    → run_pipeline()
  ├── /health             (GET)    → health.py        → {"status": "ok"}
  ├── /health/ready       (GET)    → health.py        → SupabaseProvider.check_connection()
  └── /v1/chat/...        (POST)   → api/chat.py → chat_service → graph.ainvoke()
```

### 파이프라인 진입점 (웹 경로)

```python
# app/pipeline/runner.py
@observe(name="pipeline.run")
async def run_pipeline(req: RecommendRequest) -> RecommendResponse:
    state = PipelineState.from_request(req)
    state = await embed_step(state)
    state = await search_step(state)
    state = await diversify_step(state)
    return RecommendResponse.from_state(state)
```

### LangGraph 진입점 (앱/웹 채팅 경로)

```python
# app/graphs/fashion_bot.py
graph = build_graph()  # StateGraph 컴파일, 모듈 수준 캐시

# app/services/chat_service.py
await graph.ainvoke(InputState(...), config={"callbacks": [build_callback_handler()]})
```

그래프 토폴로지 (12 노드):

```
ingest → resolve_image → vision → pick_item → ask_clarify? → apply_clarify? →
  critique_apply → search → evaluator ─┐
                                        │ score >= threshold 또는 budget 소진
                                        ├── search (retry, delta 적용)
                                        └── send_results → taste_update → respond
```

---

## 아키텍처 패턴

### 1. 이중 진입 경로

웹 경로(`POST /recommend`)는 plain async state machine(`PipelineState`)으로 직선 파이프라인을 실행한다. 앱/웹 채팅 경로(`POST /v1/chat/...`)는 LangGraph StateGraph(`WorkingState`)로 분기/루프/콜백을 처리한다. 두 경로는 `RecommendationPort` Protocol 인터페이스를 통해 동일한 검색 파이프라인을 공유한다.

### 2. Provider singleton

외부 서비스 클라이언트(`SupabaseProvider`, `EmbedProvider`, `LLMProvider`)는 클래스 변수 singleton 으로 관리한다. `lifespan` 의 startup 단계에서 Supabase async 클라이언트를 한 번 초기화해 race condition 을 제거한다.

### 3. @observe-per-step

각 pipeline step 함수와 `run_pipeline()` 전체에 `@observe` 데코레이터를 적용한다. step 단위 trace 를 Langfuse 로 전송하며, 키 미설정 시 no-op 으로 동작한다.

### 4. lifespan-managed clients

모든 외부 클라이언트 생명주기를 FastAPI `lifespan` 에서 관리한다. 요청 처리 중 클라이언트를 생성하거나 닫지 않는다.

### 5. Pydantic v2 alias 패턴

- 요청 모델: `alias` (camelCase 입력 수용) + `populate_by_name=True`
- 응답 모델: `serialization_alias` (camelCase 출력) + `model_dump(by_alias=True)`

---

## 핵심 파일 매핑

| 파일 | 역할 |
|------|------|
| `app/main.py` | FastAPI 엔트리포인트 + lifespan + CORS + ORJSONResponse + messenger adapter 워밍업 |
| `app/api/recommend.py` | `POST /recommend` (X-Internal-Token 인증) |
| `app/api/health.py` | `/health` (liveness) + `/health/ready` (readiness + messenger 상태) |
| `app/api/chat.py` | `POST /v1/chat/...` (앱 JWT 인증, SSE 스트리밍) |
| `app/channels/adapter.py` | `MessengerAdapter` ABC |
| `app/channels/factory.py` | `MESSENGER_BACKEND` 기반 어댑터 팩토리 |
| `app/channels/recommendation.py` | `RecommendationPort` Protocol + `ChannelRecommendationRequest/Result` DTO |
| `app/channels/vision.py` | GPT-4o-mini Vision 호출 → `VisionResult` 반환 (rich schema) |
| `app/channels/vision_prompt.py` | `ANALYZE_SYSTEM_PROMPT` / `ANALYZE_USER_PROMPT` 상수 (kikoai/app 동결 사본) |
| `app/channels/clarify.py` | `ClarifyAxis`, `ClarifyDelta`, `parse_callback`, `pick_clarify_axis` |
| `app/channels/clarify_values.py` | 축별 enum 값 + keywords/subcategory_override/searchQueryKo_augment 매핑 표 |
| `app/channels/session.py` | `SessionStore` Protocol + `InMemorySessionStore` 구현체 |
| `app/graphs/fashion_bot.py` | LangGraph StateGraph 빌드 + 모듈 수준 컴파일 캐시 |
| `app/graphs/state.py` | `InputState`, `WorkingState`, `OutputState` Pydantic v2 모델 |
| `app/graphs/routing.py` | 조건부 엣지 함수 (after_ingest, after_resolve_image, after_vision, after_pick, after_critique, after_search, after_evaluator) |
| `app/graphs/nodes/evaluator.py` | Reflexion 자기-비평 노드 (`CritiqueScore`, `CritiqueDelta`, fast-path, fail-open) |
| `app/graphs/nodes/apply_clarify.py` | `ClarifyDelta` → `WorkingState` 보강 → search_node 진입 |
| `app/graphs/nodes/ask_clarify.py` | `pick_clarify_axis` → 인라인 키보드 카드 전송 (LLM 0회) |
| `app/core/config.py` | pydantic-settings, 다수 feature flag 및 LLM 파라미터 |
| `app/core/auth.py` | `verify_internal_token` FastAPI dependency |
| `app/pipeline/state.py` | `PipelineState` dataclass — step 간 데이터 전달 |
| `app/pipeline/runner.py` | 파이프라인 조립 + `@observe` 전체 trace |
| `app/providers/database.py` | `SupabaseProvider` — async singleton, lifespan 워밍업 |
| `app/providers/embedding.py` | `EmbedProvider` — httpx + Modal + 응답 스키마 검증 |
| `app/providers/llm.py` | `LLMProvider` — httpx + LiteLLM proxy |
| `app/observability/langfuse.py` | `@observe` (Langfuse v2/v3 호환, no-op fallback) |
| `app/models/request.py` | `RecommendRequest` — alias + image_url SSRF guard |
| `app/models/response.py` | `RecommendResponse` — serialization_alias camelCase |
