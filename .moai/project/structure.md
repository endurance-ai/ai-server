# structure.md — portal-ai 디렉토리 구조

portal-ai 의 디렉토리 트리, 각 모듈 책임, 진입점, 아키텍처 패턴을 설명한다.

---

## 루트 트리

```
portal-ai/
├── app/                        # FastAPI 애플리케이션 본체
│   ├── main.py                 # 앱 팩토리, lifespan, CORS
│   ├── api/
│   │   ├── __init__.py         # APIRouter 조립 (include_router)
│   │   ├── recommend.py        # POST /recommend
│   │   └── health.py           # GET /health, GET /health/ready
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
│   │   └── runner.py           # 4단계 조립 + @observe (PIPELINE_PARALLEL_ENABLED 분기)
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
├── tests/                      # pytest-asyncio 테스트
│   ├── conftest.py             # httpx AsyncClient fixture
│   ├── test_health.py
│   ├── test_config.py
│   ├── test_enhance_query.py            # SPEC-PIPELINE-001 unit (15 cases)
│   └── test_pipeline_with_enhance.py    # SPEC-PIPELINE-001 integration (6 cases)
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
| `app/api/` | HTTP 라우터. recommend(인증 있음) + health(liveness/readiness) |
| `app/core/` | 환경변수 로딩(`config.py`) + 인증 dependency(`auth.py`) |
| `app/models/` | Pydantic v2 request/response 스키마. camelCase ↔ snake_case alias |
| `app/pipeline/` | 검색 파이프라인 state machine. (embed ‖ enhance_query) → search → diversify. enhance_query 는 SPEC-PIPELINE-001 도입, feature flag 기본 off |
| `app/providers/` | 외부 서비스 클라이언트 singleton. Supabase / Modal / LiteLLM |
| `app/observability/` | Langfuse `@observe` 데코레이터 래퍼. SDK 버전 호환 + no-op fallback |
| `docs/` | 아키텍처, 코드 패턴, 운영 인프라 문서 |
| `tests/` | 단위 + 통합 테스트. pytest-asyncio `mode=auto` |
| `scripts/` | 운영 이외 유틸. 로컬 배치 임베딩 스크립트 (embed group 별도 설치) |

---

## 진입점

### 서버 진입점

```
app.main:app
```

`app/main.py` 가 FastAPI 인스턴스를 생성하고 다음 세 가지를 설정한다:

- **lifespan**: startup 시 `SupabaseProvider.get_client()` 워밍업 → shutdown 시 모든 Provider close
- **CORS**: `allow_origins=["*"]`, `allow_credentials=False` (stateless)
- **default_response_class**: `ORJSONResponse` (orjson 직렬화)

### 라우터 구조

```
app/api/__init__.py
  ├── /recommend  (POST)   → recommend.py → run_pipeline()
  ├── /health     (GET)    → health.py    → {"status": "ok"}
  └── /health/ready (GET)  → health.py    → SupabaseProvider.check_connection()
```

### 파이프라인 진입점

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

---

## 아키텍처 패턴

### 1. Plain async state machine

LangGraph 를 사용하지 않는다. 모든 파이프라인 step 은 `(state: PipelineState) -> PipelineState` 시그니처를 따른다. 직선 파이프라인이므로 그래프 오버헤드가 불필요하며, 향후 분기/병렬 필요 시 LangGraph 노드로 wrap 가능하다 (마이그레이션 비용 최소화).

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
| `app/main.py` | FastAPI 엔트리포인트 + lifespan + CORS + ORJSONResponse |
| `app/api/recommend.py` | `POST /recommend` (X-Internal-Token 인증) |
| `app/api/health.py` | `/health` (liveness) + `/health/ready` (readiness) |
| `app/core/config.py` | pydantic-settings, `ALLOWED_IMAGE_HOSTS` 계산 |
| `app/core/auth.py` | `verify_internal_token` FastAPI dependency |
| `app/pipeline/state.py` | `PipelineState` dataclass — step 간 데이터 전달 |
| `app/pipeline/embed.py` | Modal `/embed` HTTP 호출 → 벡터 반환 |
| `app/pipeline/search.py` | Supabase `search_products_v5` RPC → top-50 후보 |
| `app/pipeline/diversify.py` | 브랜드/플랫폼 캡 + tolerance → top-N 최종 결과 |
| `app/pipeline/runner.py` | 파이프라인 조립 + `@observe` 전체 trace |
| `app/providers/database.py` | `SupabaseProvider` — async singleton, lifespan 워밍업 |
| `app/providers/embedding.py` | `EmbedProvider` — httpx + Modal + 응답 스키마 검증 |
| `app/providers/llm.py` | `LLMProvider` — httpx + LiteLLM proxy |
| `app/observability/langfuse.py` | `@observe` (Langfuse v2/v3 호환, no-op fallback) |
| `app/models/request.py` | `RecommendRequest` — alias + image_url SSRF guard |
| `app/models/response.py` | `RecommendResponse` — serialization_alias camelCase |
