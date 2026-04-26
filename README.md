# portal-ai-server

> Portal.ai 패션 추천 AI 서버. FastAPI 기반 검색/리파인 파이프라인.

`portal/app`(Next.js)에서 IG Vision 분석 끝난 단일 아이템을 받아, **Modal에서 이미지 임베딩 → Supabase v5 검색 RPC (dense + sparse + RRF) → 다양성 캡 → product 리스트 반환.**

```
[Vercel / Next.js]                  [EC2 t4g.medium / Docker Compose]              [Modal Serverless]
─────────────────────               ───────────────────────────────────             ─────────────────
Apify + R2 + DB                     ai-server (이 프로젝트)                          /embed (FashionSigLIP)
GPT-4o-mini Vision                  litellm + litellm-db
세션 / Auth / UI                    langfuse-web + langfuse-db
v4 검색 (폴백)
```

## 빠른 시작

```bash
uv sync
cp .env.example .env             # 값 채워넣기 — docs/infra/env.md 참고
uv run uvicorn app.main:app --reload --port 8000
curl http://localhost:8000/health  # liveness (no auth)
```

## 검증

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

## 배포

`dev` 브랜치로 PR merge 시 GitHub Actions가 자동 빌드 + ECR push + EC2 SSH deploy.
상세는 [`docs/infra/cicd.md`](docs/infra/cicd.md).

## 디렉토리

```
app/
├── main.py              # FastAPI 엔트리포인트
├── api/                 # 라우터 (recommend, health)
├── core/                # config, auth
├── pipeline/            # state machine (embed → search → diversify)
├── providers/           # SupabaseProvider, EmbedProvider, LLMProvider
├── observability/       # @observe 래퍼
└── models/              # Pydantic v2 (request/response)

docs/
├── ARCHITECTURE.md      # 전체 그림 + 토폴로지
├── PATTERNS.md          # 코드 컨벤션
├── features/
│   ├── pipeline.md      # state machine 상세
│   ├── search-engine.md # v5 RPC + RRF + 다양성
│   └── observability.md # Langfuse 통합
└── infra/
    ├── env.md           # 환경변수 매트릭스
    ├── deployment.md    # EC2 docker-compose + Modal
    └── cicd.md          # GitHub Actions + ECR + SSH
```

## 핵심 기술

| 영역 | 선택 |
|------|------|
| 프레임워크 | FastAPI + uvicorn |
| 벡터/풀텍스트 검색 | Supabase pgvector(HNSW) + pgroonga (Qdrant 미사용) |
| 이미지 임베딩 | Modal serverless — `Marqo/marqo-fashionSigLIP` (T4) |
| LLM | LiteLLM proxy 경유 (httpx) |
| 관측성 | Langfuse self-host (LiteLLM callback + `@observe`) |
| 스키마 | Pydantic v2 |
| 패키지/린트/테스트 | uv / ruff / pytest |
| 컨테이너 | Docker (multi-stage uv) |

## 관련 프로젝트

| 프로젝트 | 경로 | 역할 |
|---------|------|------|
| portal/app | `/Users/hansangho/Desktop/portal/app` | Next.js 모놀리스 (caller + v4 폴백) |
| aws-infra | `/Users/hansangho/Desktop/aws-infra/portal-ai-servers/portal-ai/` | EC2 docker-compose + Modal 배포 |

## 라이선스

Internal — Portal.ai 팀 전용.
