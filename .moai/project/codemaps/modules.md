# portal-ai 모듈 책임 및 공개 인터페이스

## 모듈 일람

| 모듈 | 책임 | 공개 함수·클래스 | 주요 의존 |
|------|------|------------------|-----------|
| `app.main` | FastAPI 앱 생성, lifespan 훅, CORS, 라우터 등록 | `app` (FastAPI 인스턴스), `lifespan` | `app.api`, `app.core.config`, `app.providers.*` |
| `app.api.recommend` | `POST /recommend` 엔드포인트 처리 | `recommend(req)` | `app.core.auth`, `app.models.*`, `app.pipeline.runner` |
| `app.api.health` | `/health` (liveness), `/health/ready` (readiness) | `health_live()`, `health_ready()` | `app.core.auth`, `app.providers.*` |
| `app.core.config` | 환경변수 로딩, 설정 싱글톤 | `Settings`, `get_settings()`, `settings` | `pydantic-settings` |
| `app.core.auth` | X-Internal-Token 헤더 검증 FastAPI dependency | `verify_internal_token` | `app.core.config` |
| `app.pipeline.state` | 파이프라인 단계 간 가변 상태 컨테이너 | `PipelineState` (dataclass) | `app.models.request` |
| `app.pipeline.embed` | Modal `/embed` 호출, embedding 벡터 저장 | `embed_step(state)` | `app.providers.embedding`, `app.observability.langfuse` |
| `app.pipeline.enhance_query` | LLM 기반 sparse 쿼리 정제 (LiteLLM 경유, feature flag 기본 off, 모든 실패 raw 폴백) | `enhance_query_step(state)` | `app.providers.llm`, `app.core.config`, `app.observability.langfuse` |
| `app.pipeline.search` | Supabase `search_products_v5` RPC 호출, raw_candidates 저장. `state.enhance_query_status=="ok"` 면 refined query 우선 사용 | `search_step(state)` | `app.providers.database`, `app.core.config`, `app.observability.langfuse` |
| `app.pipeline.diversify` | 브랜드·플랫폼 다양성 캡 적용, final_candidates 선정 | `diversify_step(state)`, `_tolerance_to_target_count(tolerance)` | `app.core.config`, `app.observability.langfuse` |
| `app.pipeline.runner` | 4단계 조립 (embed ‖ enhance_query → search → diversify), `PIPELINE_PARALLEL_ENABLED` 분기, Langfuse trace 루트, RecommendResponse 생성 | `run_pipeline(req)` | `app.pipeline.*`, `app.models.*`, `app.observability.langfuse` |
| `app.providers.database` | Supabase async 클라이언트 싱글톤, RPC 헬퍼 | `SupabaseProvider` (classmethod: `get_client`, `check_connection`, `rpc`, `close`) | `supabase`, `app.core.config` |
| `app.providers.embedding` | Modal HTTP 클라이언트 싱글톤, 단건/배치 임베딩 | `EmbedProvider` (classmethod: `get_client`, `embed_image_url`, `embed_image_urls`, `check_connection`, `close`) | `httpx`, `app.core.config` |
| `app.providers.llm` | LiteLLM proxy HTTP 클라이언트 싱글톤. `enhance_query_step` 의 첫 사용처 (SPEC-PIPELINE-001) | `LLMProvider` (classmethod: `chat`, `check_connection`, `close`) | `httpx`, `app.core.config` |
| `app.observability.langfuse` | `@observe` 데코레이터 — Langfuse v2/v3 호환, no-op 폴백 | `observe(name)` | `app.core.config`, `langfuse` (optional) |
| `app.models.request` | 요청 스키마 정의, SSRF 검증 | `RecommendRequest`, `AnalyzedItem`, `PriceFilter`, `StyleNode` | `pydantic`, `app.core.config` |
| `app.models.response` | 응답 스키마 정의, serialization_alias | `RecommendResponse`, `Candidate` | `pydantic` |

## 모듈별 상세

### app.main

- `lifespan`: asynccontextmanager. 시작 시 `SupabaseProvider.get_client()` 워밍업(첫 요청 race condition 방지), 종료 시 세 Provider 모두 `close()`.
- `app`: `default_response_class=ORJSONResponse` 설정으로 직렬화 성능 확보.
- CORS: `allow_origins=["*"]` (stateless, 세션은 portal/app이 관리).

### app.pipeline.state

- `PipelineState` dataclass: `request`, `embedding`, `raw_candidates`, `final_candidates`, `counts`, `latency_ms` 필드.
- `start(step)` / `end(step)`: `perf_counter` 기반 단계별 latency 측정.
- 웹 경로 전용. Telegram 경로는 `app/graphs/state.py::WorkingState`(Pydantic v2)를 사용한다.

### app.core.config

- `Settings`: `pydantic-settings BaseSettings` 상속. `.env` 파일 자동 로딩.
- `allowed_image_hosts` property: `ALLOWED_IMAGE_HOSTS` 콤마 구분 문자열 → `list[str]` 변환.
- `get_settings()`: `@lru_cache`로 싱글톤 보장.
- 주요 파라미터 기본값: `SEARCH_DEFAULT_K=50`, `SEARCH_BRAND_CAP=2`, `SEARCH_PLATFORM_CAP=3`, `SEARCH_FINAL_LIMIT=15`, `MODAL_EMBED_TIMEOUT=90.0`.

### app.models.request

- `AnalyzedItem`: portal/app이 GPT-4o-mini Vision으로 검출한 단일 아이템. `searchQuery`/`searchQueryKo` camelCase alias 지원.
- `RecommendRequest`: `image_url` SSRF 가드 내장 (`@field_validator`). `tolerance` 범위 0.0~1.0. `final_limit` 범위 1~50.

### app.models.response

- `Candidate`: `imageUrl`, `productUrl`, `denseRank`, `sparseRank` serialization_alias (camelCase) — portal/app 컨벤션 맞춤.
- `RecommendResponse`: `itemId`, `latencyMs` camelCase alias. `counts` 딕셔너리로 단계별 후보 수 노출.
