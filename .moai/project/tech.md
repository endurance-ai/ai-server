# tech.md — kiko.ai 기술 스택

kiko.ai 의 런타임, 의존성, 외부 서비스, 환경변수, 빌드/배포, 개발 명령, 알려진 제약조건을 정리한다.

---

## 런타임 / 프레임워크

| 항목 | 기술 | 비고 |
|------|------|------|
| 언어 | Python 3.13+ | JIT 실험 미사용, GIL-free 미사용 |
| 웹 프레임워크 | FastAPI 0.115+ | lifespan, ORJSONResponse, Depends |
| ASGI 서버 | uvicorn 0.34+ (standard) | watchfiles 포함 (--reload 지원) |
| 데이터 검증 | Pydantic v2 (2.10+) | model_validate, alias, serialization_alias |
| 설정 관리 | pydantic-settings 2.13.1+ | .env 자동 로딩, ALLOWED_IMAGE_HOSTS 계산 |

---

## 의존성

### 운영 의존성 (`pyproject.toml` `[project]`)

| 패키지 | 버전 범위 | 용도 |
|--------|----------|------|
| `fastapi` | >=0.115 | 웹 프레임워크 |
| `uvicorn[standard]` | >=0.34 | ASGI 서버 |
| `pydantic` | >=2.10 | 데이터 모델 + 검증 |
| `pydantic-settings` | >=2.13.1 | 환경변수 설정 |
| `httpx` | >=0.28 | Modal + LiteLLM async HTTP 클라이언트 |
| `orjson` | >=3.10 | 고성능 JSON 직렬화 (ORJSONResponse) |
| `supabase` | >=2.10 | Supabase async 클라이언트 (supabase-py) |
| `langfuse` | >=2.50,<3.0 | Observability — v2 고정 (서버 호환 이유 있음) |

### 개발 의존성 (`[dependency-groups] dev`)

| 패키지 | 버전 범위 | 용도 |
|--------|----------|------|
| `ruff` | >=0.11 | 린트 + 포맷 |
| `pytest` | >=8.0 | 테스트 러너 |
| `pytest-asyncio` | >=0.25 | async 테스트 (`asyncio_mode=auto`) |

### 로컬 배치 임베딩 전용 (`[dependency-groups] embed`)

운영 이미지에는 포함하지 않는다. `uv sync --group embed` 로 별도 설치 (약 5GB, Mac M-series 는 MPS 자동 사용).

| 패키지 | 버전 범위 | 용도 |
|--------|----------|------|
| `torch` | >=2.4 | 로컬 추론 엔진 |
| `open-clip-torch` | >=2.26 | FashionSigLIP 로컬 실행 |
| `Pillow` | >=10.0 | 이미지 전처리 |
| `tqdm` | >=4.60 | 배치 진행 표시 |

---

## 외부 서비스

### Modal (FashionSigLIP 임베딩)

- 역할: 이미지 → 벡터 변환. T4 GPU scale-to-zero 환경.
- 진입점: `EmbedProvider.embed_image_url()` → `POST {MODAL_EMBED_URL}/embed`
- 응답 검증: `embedding` 키가 비어있거나 없으면 즉시 `ValueError` 발생 (신뢰하지 않음)
- 제약: 콜드스타트 최대 90초. `MODAL_EMBED_TIMEOUT=90` 설정 필수.

### Supabase (pgvector + pgroonga)

- 역할: `search_products_v5` RPC 호출. dense(HNSW) + sparse(pgroonga BM25) + RRF 결합 → top-50 반환.
- 진입점: `SupabaseProvider.rpc("search_products_v5", params)`
- 클라이언트: `supabase-py` async, lifespan 에서 singleton 워밍업.
- 제약: HNSW 타임아웃 위험. 배치 처리 시 chunk 크기 25 이하, 타임아웃 시 자동 분할 재시도.
- 의존: `kikoai/app/supabase/migrations/030_search_products_v5.sql` 스키마에 강하게 결합. RPC 스키마 변경 시 kiko.ai 코드 동시 수정 필요.

### LiteLLM proxy

- 역할: LLM 호출(chat completion, rerank) 추상화 레이어. 현재 `enhance_query` 는 백로그.
- 진입점: `LLMProvider.chat()` → `POST {LITELLM_BASE_URL}/chat/completions`
- 관측: `litellm-config.yaml` 의 `success_callback: ["langfuse"]` 로 모든 LLM 호출 자동 trace.

### Langfuse (Observability)

- 역할: 파이프라인 step 단위 trace SSOT.
- 위치: EC2 t4g.medium 위 docker-compose (AI 서버 옆 컨테이너). 별도 Postgres.
- 버전: v2 이미지 고정. Python SDK `langfuse>=2.50,<3.0` (v3 SDK 는 ingestion endpoint 변경으로 404 발생).
- 코드 패턴: `app/observability/langfuse.py` 의 `@observe` 데코레이터. 키 미설정 시 no-op 자동 폴백.

---

## 환경변수 정책

`.env` 파일(개발) 또는 EC2 환경변수(운영)로 주입. 운영 전환 시 AWS Parameter Store 로 이전 예정.

### 필수 환경변수

| 변수 | 설명 |
|------|------|
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role 키 (RPC 권한 필요) |
| `MODAL_EMBED_URL` | Modal `/embed` 엔드포인트 기본 URL |
| `MODAL_EMBED_TIMEOUT` | Modal 타임아웃(초). 콜드스타트 고려 90 권장 |
| `INTERNAL_API_TOKEN` | X-Internal-Token 검증용 shared secret |
| `LITELLM_BASE_URL` | LiteLLM proxy 기본 URL |

### 선택 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `ALLOWED_IMAGE_HOSTS` | (없음) | image_url SSRF 허용 호스트 목록 (쉼표 구분). 운영: R2 도메인만 허용 |
| `LANGFUSE_PUBLIC_KEY` | (없음) | 미설정 시 no-op 폴백 |
| `LANGFUSE_SECRET_KEY` | (없음) | 미설정 시 no-op 폴백 |
| `LANGFUSE_HOST` | (없음) | self-host URL |

### ALLOWED_IMAGE_HOSTS 상세

`pydantic-settings` 에서 쉼표 구분 문자열을 파싱해 Python `set` 으로 변환한다. `RecommendRequest.image_url` 의 `field_validator` 가 이 set 을 기준으로 호스트 화이트리스트 검증을 수행한다(SSRF 방어).

```bash
# 예시 — Cloudflare R2 도메인 허용
ALLOWED_IMAGE_HOSTS=pub-xxx.r2.dev,r2.cloudflarestorage.com
```

### X-Internal-Token

`POST /recommend` 와 `GET /health/ready` 는 `X-Internal-Token` 헤더를 요구한다. `verify_internal_token` dependency 가 `INTERNAL_API_TOKEN` 환경변수와 비교한다. 미설정 시 검증을 스킵한다(개발 환경 편의).

---

## 빌드 / 배포

### Docker (multi-stage uv 빌드)

```dockerfile
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

FROM python:3.13-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app /app/app
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- `--no-dev`: 운영 이미지에 개발 의존성 포함 안 함
- `embed` group 도 제외됨 (로컬 전용)

### CI/CD

GitHub Actions → ECR push → EC2 SSH 배포.

- 빌드 아키텍처: `linux/arm64` 네이티브 러너 (EC2 t4g.medium arm64 대응)
- ECR: AWS ECR 에 이미지 push
- 배포: EC2 SSH 접속 → `docker compose pull && docker compose up -d`
- 인프라 정의: `aws-infra/kiko-ai-servers/portal-ai/` (별도 레포)

### 운영 환경

- 서버: EC2 t4g.medium (arm64)
- 구성: `aws-infra` 레포의 docker-compose. AI 서버 + LiteLLM proxy + Langfuse web + Langfuse Postgres.
- Modal: AWS 외부 (scale-to-zero GPU). weights 는 Modal Volume 에 캐시.
- Supabase: 외부 관리형 Postgres (pgvector + pgroonga).

---

## 개발 명령

```bash
# 의존성 설치
uv sync

# 로컬 배치 임베딩 의존성 추가 설치 (~5GB)
uv sync --group embed

# 로컬 서버 실행 (hot reload)
uv run uvicorn app.main:app --reload --port 8000

# 린트 + 포맷
uv run ruff check .
uv run ruff format .

# CI 검증용 포맷 확인 (변경 없음)
uv run ruff format --check .

# 테스트
uv run pytest
uv run pytest -q   # 간결 출력

# 로컬 스택 실행 (docker-compose)
docker compose up -d
```

### ruff 설정 요약

```toml
target-version = "py313"
line-length = 120
select = ["E", "F", "I", "N", "W", "UP"]
```

---

## 알려진 제약조건

### Modal 콜드스타트

- 현상: FashionSigLIP GPU 컨테이너가 scale-to-zero 상태일 때 `/embed` 첫 호출이 최대 90초 소요.
- 대응: `MODAL_EMBED_TIMEOUT=90` 으로 타임아웃 설정. 현재 콜드스타트 실패 시 502 → kikoai/app v4 폴백.
- 미래 개선: sparse-only 폴백 모드 (Priority High 로드맵 항목).

### Supabase HNSW 타임아웃

- 현상: `search_products_v5` RPC 의 HNSW 인덱스 탐색이 배치 크기가 클 때 타임아웃.
- 대응: 배치 chunk 크기를 25 이하로 제한. 타임아웃 발생 시 자동 분할 재시도.
- 단건 `/recommend` 는 해당 없음. 배치 처리 스크립트(`embed_batch_local.py`) 에서 관리.

### Langfuse v2 SDK 고정

- 현상: self-host 서버가 Langfuse v2 docker 이미지 사용. v3 Python SDK 는 ingestion endpoint 경로가 변경되어 404 오류 발생.
- 대응: `pyproject.toml` 에 `langfuse>=2.50,<3.0` 고정. 서버 이미지를 v3 로 업그레이드하기 전에는 SDK 를 올리지 않는다.
- 업그레이드 조건: EC2 docker-compose 의 Langfuse 서버 이미지를 v3 로 교체 후 SDK 잠금 해제.

### Supabase RPC 스키마 의존

- 현상: `search_products_v5` RPC 파라미터/반환 스키마가 변경되면 `app/pipeline/search.py` 가 즉시 영향을 받음.
- 대응: RPC 스키마 변경 시 kiko.ai 코드와 동시 배포 필요. 마이그레이션 파일: `kikoai/app/supabase/migrations/030_search_products_v5.sql`.
