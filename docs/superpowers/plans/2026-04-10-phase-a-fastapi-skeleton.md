# Phase A: FastAPI 앱 뼈대 + Health Check 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** FastAPI AI 서버의 기본 뼈대를 세우고, Qdrant/LiteLLM 연결 상태를 확인하는 /health 엔드포인트를 구현한다.

**Architecture:** lifespan context manager로 Qdrant/LiteLLM 클라이언트를 초기화하고, 싱글톤 provider로 관리. health 엔드포인트에서 두 서비스의 연결 상태를 확인하여 리턴. Docker Compose로 ai-server + litellm + qdrant를 한 번에 기동 가능하게 구성.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, AsyncQdrantClient, LiteLLM (httpx), Pydantic v2, Docker Compose

**완료 기준:** `curl localhost:8000/health` → `{"status": "ok", "qdrant": "connected", "litellm": "connected"}`

---

## File Structure

```
app/
├── __init__.py
├── main.py                 # FastAPI 앱 + lifespan + CORS + exception handlers
├── core/
│   ├── __init__.py
│   └── config.py           # Pydantic BaseSettings (환경변수)
├── api/
│   ├── __init__.py         # APIRouter 조립
│   └── health.py           # GET /health
└── providers/
    ├── __init__.py
    ├── vector.py           # AsyncQdrantClient 싱글톤
    └── llm.py              # LiteLLM 프록시 클라이언트
tests/
├── __init__.py
├── conftest.py             # TestClient fixture
├── test_config.py          # Settings 테스트
└── test_health.py          # /health 엔드포인트 테스트
Dockerfile
docker-compose.yml
litellm-config.yaml
```

---

### Task 1: 디렉토리 구조 + Config

**Files:**
- Create: `app/__init__.py`, `app/core/__init__.py`, `app/api/__init__.py`, `app/providers/__init__.py`
- Create: `app/core/config.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_config.py`

- [ ] **Step 1: 디렉토리 + __init__.py 생성**

```bash
mkdir -p app/core app/api app/providers app/models app/pipeline app/scoring tests
touch app/__init__.py app/core/__init__.py app/api/__init__.py app/providers/__init__.py
touch app/models/__init__.py app/pipeline/__init__.py app/scoring/__init__.py
touch tests/__init__.py
```

- [ ] **Step 2: config.py 테스트 작성**

`tests/test_config.py`:
```python
from app.core.config import Settings


def test_settings_defaults():
    """기본값으로 Settings 생성 가능해야 한다."""
    s = Settings(
        QDRANT_HOST="localhost",
        QDRANT_PORT=6333,
        LITELLM_BASE_URL="http://localhost:4000",
    )
    assert s.QDRANT_HOST == "localhost"
    assert s.QDRANT_PORT == 6333
    assert s.LITELLM_BASE_URL == "http://localhost:4000"
    assert s.PROJECT_NAME == "kiko-ai-server"
    assert s.ENVIRONMENT == "development"


def test_settings_qdrant_url():
    """qdrant_url 프로퍼티가 host:port를 결합해야 한다."""
    s = Settings(
        QDRANT_HOST="qdrant",
        QDRANT_PORT=6333,
        LITELLM_BASE_URL="http://litellm:4000",
    )
    assert s.qdrant_url == "http://qdrant:6333"
```

- [ ] **Step 3: 테스트 실행 → 실패 확인**

```bash
uv run pytest tests/test_config.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.config'`

- [ ] **Step 4: config.py 구현**

`app/core/config.py`:
```python
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    PROJECT_NAME: str = "kiko-ai-server"
    VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    # Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333

    # LiteLLM Proxy
    LITELLM_BASE_URL: str = "http://localhost:4000"

    @property
    def qdrant_url(self) -> str:
        return f"http://{self.QDRANT_HOST}:{self.QDRANT_PORT}"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
```

- [ ] **Step 5: pydantic-settings 의존성 추가**

`pydantic-settings`는 `pydantic`과 별도 패키지:
```bash
uv add pydantic-settings
```

- [ ] **Step 6: 테스트 실행 → 통과 확인**

```bash
uv run pytest tests/test_config.py -v
```
Expected: 2 passed

- [ ] **Step 7: 커밋**

```bash
git add app/ tests/ pyproject.toml uv.lock
git commit -m "feat: 프로젝트 디렉토리 구조 + Settings 설정"
```

---

### Task 2: Qdrant Provider

**Files:**
- Create: `app/providers/vector.py`

- [ ] **Step 1: vector.py 구현**

Qdrant 연결 확인은 /health에서만 사용하므로, 단위 테스트 없이 provider를 먼저 작성.
health 엔드포인트 테스트 (Task 4)에서 통합 검증.

`app/providers/vector.py`:
```python
from typing import ClassVar

from qdrant_client import AsyncQdrantClient

from app.core.config import settings


class VectorProvider:
    """Qdrant 벡터 DB 클라이언트 (싱글톤)"""

    _client: ClassVar[AsyncQdrantClient | None] = None

    @classmethod
    def get_client(cls) -> AsyncQdrantClient:
        if cls._client is None:
            cls._client = AsyncQdrantClient(
                url=settings.qdrant_url,
                timeout=10,
            )
        return cls._client

    @classmethod
    async def check_connection(cls) -> bool:
        """Qdrant 연결 확인. 연결되면 True, 실패하면 False."""
        try:
            client = cls.get_client()
            await client.get_collections()
            return True
        except Exception:
            return False

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            await cls._client.close()
            cls._client = None
```

- [ ] **Step 2: 커밋**

```bash
git add app/providers/vector.py
git commit -m "feat: Qdrant VectorProvider (싱글톤 AsyncQdrantClient)"
```

---

### Task 3: LiteLLM Provider

**Files:**
- Create: `app/providers/llm.py`

- [ ] **Step 1: llm.py 구현**

LiteLLM 프록시 연결 확인. httpx로 /health 엔드포인트 호출.

`app/providers/llm.py`:
```python
from typing import ClassVar

import httpx

from app.core.config import settings


class LLMProvider:
    """LiteLLM 프록시 클라이언트"""

    _client: ClassVar[httpx.AsyncClient | None] = None

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            cls._client = httpx.AsyncClient(
                base_url=settings.LITELLM_BASE_URL,
                timeout=10.0,
            )
        return cls._client

    @classmethod
    async def check_connection(cls) -> bool:
        """LiteLLM 프록시 연결 확인."""
        try:
            client = cls.get_client()
            resp = await client.get("/health")
            return resp.status_code == 200
        except Exception:
            return False

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
```

- [ ] **Step 2: 커밋**

```bash
git add app/providers/llm.py
git commit -m "feat: LiteLLM LLMProvider (httpx 프록시 클라이언트)"
```

---

### Task 4: Health 엔드포인트

**Files:**
- Create: `app/api/health.py`
- Create: `tests/test_health.py`, `tests/conftest.py`

- [ ] **Step 1: conftest.py 작성 (TestClient fixture)**

`tests/conftest.py`:
```python
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

- [ ] **Step 2: health 테스트 작성**

`tests/test_health.py`:
```python
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient


async def test_health_all_connected(client: AsyncClient):
    """Qdrant, LiteLLM 모두 연결되면 status=ok."""
    with (
        patch("app.api.health.VectorProvider.check_connection", new_callable=AsyncMock, return_value=True),
        patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=True),
    ):
        resp = await client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["qdrant"] == "connected"
    assert data["litellm"] == "connected"


async def test_health_qdrant_down(client: AsyncClient):
    """Qdrant 다운 시 status=degraded."""
    with (
        patch("app.api.health.VectorProvider.check_connection", new_callable=AsyncMock, return_value=False),
        patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=True),
    ):
        resp = await client.get("/health")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["qdrant"] == "disconnected"


async def test_health_litellm_down(client: AsyncClient):
    """LiteLLM 다운 시 status=degraded."""
    with (
        patch("app.api.health.VectorProvider.check_connection", new_callable=AsyncMock, return_value=True),
        patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=False),
    ):
        resp = await client.get("/health")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["litellm"] == "disconnected"
```

- [ ] **Step 3: 테스트 실행 → 실패 확인**

```bash
uv run pytest tests/test_health.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 4: health.py 구현**

`app/api/health.py`:
```python
from fastapi import APIRouter
from fastapi.responses import ORJSONResponse

from app.core.config import settings
from app.providers.llm import LLMProvider
from app.providers.vector import VectorProvider

router = APIRouter()


@router.get("/health")
async def health_check() -> ORJSONResponse:
    qdrant_ok = await VectorProvider.check_connection()
    litellm_ok = await LLMProvider.check_connection()

    all_ok = qdrant_ok and litellm_ok
    status_code = 200 if all_ok else 503

    return ORJSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if all_ok else "degraded",
            "qdrant": "connected" if qdrant_ok else "disconnected",
            "litellm": "connected" if litellm_ok else "disconnected",
            "version": settings.VERSION,
        },
    )
```

- [ ] **Step 5: api/__init__.py에 라우터 등록**

`app/api/__init__.py`:
```python
from fastapi import APIRouter

from app.api.health import router as health_router

router = APIRouter()
router.include_router(health_router, tags=["system"])
```

- [ ] **Step 6: main.py 최소 구현 (테스트 통과용)**

`app/main.py`:
```python
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api import router
from app.core.config import settings
from app.providers.llm import LLMProvider
from app.providers.vector import VectorProvider


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup
    yield
    # Shutdown
    await VectorProvider.close()
    await LLMProvider.close()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
```

- [ ] **Step 7: 테스트 실행 → 통과 확인**

```bash
uv run pytest tests/test_health.py -v
```
Expected: 3 passed

- [ ] **Step 8: 커밋**

```bash
git add app/api/ app/main.py tests/
git commit -m "feat: /health 엔드포인트 + FastAPI 앱 뼈대"
```

---

### Task 5: Docker + 인프라 설정

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `litellm-config.yaml`

- [ ] **Step 1: Dockerfile 작성**

`Dockerfile`:
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
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: docker-compose.yml 작성**

`docker-compose.yml`:
```yaml
services:
  ai-server:
    build: .
    ports:
      - "8000:8000"
    environment:
      - QDRANT_HOST=qdrant
      - QDRANT_PORT=6333
      - LITELLM_BASE_URL=http://litellm:4000
    depends_on:
      - litellm
      - qdrant
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 1536M

  litellm:
    image: ghcr.io/berriai/litellm:main
    ports:
      - "4000:4000"
    volumes:
      - ./litellm-config.yaml:/app/config.yaml
    env_file:
      - .env
    command: ["--config", "/app/config.yaml"]
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M

  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant-data:/qdrant/storage
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M

volumes:
  qdrant-data:
```

- [ ] **Step 3: litellm-config.yaml 작성**

`litellm-config.yaml`:
```yaml
model_list:
  - model_name: "gpt-4o-mini"
    litellm_params:
      model: "openai/gpt-4o-mini"
      api_key: "os.environ/OPENAI_API_KEY"

  - model_name: "embedding"
    litellm_params:
      model: "cohere/embed-v4"
      api_key: "os.environ/COHERE_API_KEY"

general_settings:
  master_key: "os.environ/LITELLM_MASTER_KEY"
```

- [ ] **Step 4: 커밋**

```bash
git add Dockerfile docker-compose.yml litellm-config.yaml
git commit -m "feat: Dockerfile + Docker Compose + LiteLLM 설정"
```

---

### Task 6: 로컬 스모크 테스트 + 정리

**Files:**
- 수정 없음 (실행 확인만)

- [ ] **Step 1: 로컬에서 FastAPI 실행 확인**

```bash
uv run uvicorn app.main:app --port 8000 &
sleep 2
curl -s http://localhost:8000/health | python -m json.tool
kill %1
```

Expected (Qdrant/LiteLLM 미기동 상태):
```json
{
    "status": "degraded",
    "qdrant": "disconnected",
    "litellm": "disconnected",
    "version": "0.1.0"
}
```
→ 503이지만 앱 자체는 정상 기동 확인.

- [ ] **Step 2: 전체 테스트 실행**

```bash
uv run pytest -v
```
Expected: All tests passed

- [ ] **Step 3: ruff 린트 확인**

```bash
uv run ruff check . && uv run ruff format --check .
```
Expected: 에러 없음

- [ ] **Step 4: 최종 커밋 + push**

```bash
git add -A  # 혹시 빠진 파일 확인 후
git push origin main
```

---

## Phase A 완료 후 다음 단계

Phase B (Qdrant + 임베딩 배치)로 진행:
1. Qdrant 컬렉션 생성 스크립트
2. Supabase에서 26K 상품 데이터 export
3. 임베딩 배치 스크립트 (scripts/batch_embed.py)
4. 배치 실행 + 검색 테스트
