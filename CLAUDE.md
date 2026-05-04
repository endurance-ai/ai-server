# portal-ai-server

Portal.ai 패션 추천 AI 서버 — FastAPI 기반 검색/리파인 파이프라인.

`portal/app`(Next.js)이 IG 분석 + Vision 처리까지 끝낸 단일 아이템을 받아, **Modal에서 이미지 임베딩 → Supabase v5 검색 RPC → 다양성 캡 → product_id[] 반환**.

상세 문서:
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 전체 그림 + 토폴로지
- [docs/PATTERNS.md](docs/PATTERNS.md) — 코드 컨벤션
- [docs/features/](docs/features/) — pipeline / search-engine / observability
- [docs/infra/](docs/infra/) — env / deployment / cicd

## 책임 분리 (요약)

| 레이어 | 책임 |
|--------|------|
| Vercel / `portal/app` | Apify, R2, Vision(GPT-4o-mini), 세션, UI, v4 폴백 |
| **portal/ai (이 프로젝트)** | **검색 오케스트레이션, enhance_query, Langfuse trace** |
| Modal | FashionSigLIP 임베딩 (단건 + 배치) |
| Supabase | pgvector + pgroonga, `search_products_v5` RPC |

## 디렉토리

```
app/
├── main.py              # FastAPI 앱 + lifespan + CORS
├── api/                 # 라우터 (recommend, health)
├── pipeline/            # state machine (embed → search → diversify)
├── providers/           # SupabaseProvider, EmbedProvider, LLMProvider
├── observability/       # Langfuse @observe 래퍼
├── models/              # Pydantic request/response
└── core/                # config (env)
```

## 기술 스택

| 영역 | 기술 |
|------|------|
| 프레임워크 | FastAPI + uvicorn |
| LLM | LiteLLM proxy 경유 (httpx) |
| 임베딩 | Modal HTTP endpoint (FashionSigLIP) |
| 벡터 DB | **Supabase pgvector + pgroonga** (Qdrant 미사용) |
| Observability | **Langfuse self-host** |
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

- **plain async + state → state** (LangGraph 보류, 마이그레이션 비용 0 유지)
- Pydantic v2 모델로 request/response 정의
- LLM 호출은 LiteLLM 프록시 경유 (`LITELLM_BASE_URL`)
- 임베딩 호출은 Modal endpoint (`MODAL_EMBED_URL`)
- Supabase 쿼리는 RPC 함수 호출 (`supabase-py` async)
- ruff 린트+포맷 (line-length=120)

## 핵심 파일

| 파일 | 설명 |
|------|------|
| `app/main.py` | FastAPI 엔트리포인트 + lifespan (Supabase 워밍업) |
| `app/api/recommend.py` | `POST /recommend` (X-Internal-Token 인증) |
| `app/api/health.py` | `/health` (liveness, no auth) + `/health/ready` (인증) |
| `app/core/auth.py` | `verify_internal_token` FastAPI dependency |
| `app/pipeline/state.py` | PipelineState 정의 |
| `app/pipeline/embed.py` | Modal /embed 호출 |
| `app/pipeline/enhance_query.py` | LLM 기반 sparse 쿼리 정제 (SPEC-PIPELINE-001, feature flag 기본 off) |
| `app/pipeline/search.py` | Supabase `search_products_v5` RPC |
| `app/pipeline/diversify.py` | 다양성 캡 + tolerance |
| `app/pipeline/runner.py` | 파이프라인 조립 + `@observe` |
| `app/providers/database.py` | SupabaseProvider (async, lifespan 워밍업) |
| `app/providers/embedding.py` | Modal HTTP + 응답 스키마 검증 |
| `app/providers/llm.py` | LiteLLM HTTP |
| `app/observability/langfuse.py` | `@observe` (no-op fallback) |
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

## 관련 프로젝트

| 프로젝트 | 경로 | 역할 |
|----------|------|------|
| portal/app | `/Users/hansangho/Desktop/portal/app` | Next.js 모놀리스 (caller + v4 폴백) |
| aws-infra | `/Users/hansangho/Desktop/aws-infra/portal-ai-servers/portal-ai/` | EC2 docker-compose + Langfuse + Modal 인프라 |

## 인증 구조

AI 서버는 stateless. 인증 없음.
`portal/app`이 세션 + Supabase Auth 담당, AI 서버에 request body로 전달.
