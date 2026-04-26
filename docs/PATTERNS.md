# 코드 패턴

> portal-ai-server 코드 컨벤션. 변경 시 같이 업데이트: 본 문서, `CLAUDE.md`, `docs/ARCHITECTURE.md`.

## 1. plain async + state → state

LangGraph **사용 안 함**. 모든 파이프라인 step은 `(state) -> state` 시그니처.

```python
# app/pipeline/<step>.py
@observe(name="pipeline.<step>")
async def <step>_step(state: PipelineState) -> PipelineState:
    state.start("<step>")
    # ... 작업 ...
    state.end("<step>")
    return state
```

이유:
- portal-ai 의 파이프라인이 직선이라 그래프 오버헤드 불필요
- 향후 분기/병렬/체크포인트가 필요해지면 LangGraph 마이그레이션 비용은 0 (각 step 함수는 노드로 그대로 wrap 가능)

전체 파이프라인은 `app/pipeline/runner.py` 의 `run_pipeline()` 참조.

## 2. Pydantic v2 스키마

- **request 모델**: `alias` (camelCase 받기) + `populate_by_name=True`
- **response 모델**: `serialization_alias` (camelCase 출력) + `model_dump(by_alias=True)`

```python
class RecommendRequest(BaseModel):
    image_url: str = Field(alias="imageUrl")
    tolerance: float = Field(default=0.5, ge=0.0, le=1.0)
    final_limit: int | None = Field(default=None, alias="finalLimit", ge=1, le=50)

    model_config = {"populate_by_name": True}
```

```python
class Candidate(BaseModel):
    image_url: str | None = Field(default=None, serialization_alias="imageUrl")
    dense_rank: int | None = Field(default=None, serialization_alias="denseRank")
```

## 3. Provider 싱글톤

외부 서비스 클라이언트(`SupabaseProvider`, `EmbedProvider`, `LLMProvider`)는 **클래스 변수 싱글톤**.

```python
class SomeProvider:
    _client: ClassVar[SomeClient | None] = None

    @classmethod
    async def get_client(cls) -> SomeClient:
        if cls._client is None:
            cls._client = await create_client(...)  # async 인 경우 race 주의 — 아래 lifespan 워밍업으로 해결
        return cls._client

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            await cls._client.close()
            cls._client = None
```

### lifespan 워밍업

`app/main.py` 의 `lifespan` 에서 async 클라이언트는 **startup 시 한 번 호출**해서 race condition 회피:

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
        await SupabaseProvider.get_client()
    yield
    await SupabaseProvider.close()
    await EmbedProvider.close()
    await LLMProvider.close()
```

## 4. 외부 호출 컨벤션

| 호출 | 경유 |
|------|------|
| LLM (chat completion, rerank) | LiteLLM proxy (`LITELLM_BASE_URL`) — `LLMProvider.chat()` |
| 이미지 임베딩 | Modal HTTP endpoint (`MODAL_EMBED_URL`) — `EmbedProvider.embed_image_url()` |
| Supabase RPC / 테이블 | `SupabaseProvider.rpc(fn, params)` (supabase-py async) |

**원칙:** 외부 라이브러리 직접 import 하지 않고 Provider 메서드를 통해 호출 — 테스트 시 mock 주입이 쉽고, 모델/엔드포인트 교체 시 변경점이 한 곳.

## 5. 관측성 — `@observe`

Langfuse `@observe` 데코레이터로 함수 단위 trace. `LANGFUSE_PUBLIC_KEY`/`SECRET_KEY` 미설정 시 **no-op 자동 폴백** (테스트/dev 안전).

```python
from app.observability.langfuse import observe

@observe(name="pipeline.search")
async def search_step(state: PipelineState) -> PipelineState:
    ...
```

LiteLLM 측 LLM 호출은 LiteLLM config 의 `success_callback: ["langfuse"]` 로 **자동 trace** — 코드 추가 불필요.

상세: [`features/observability.md`](features/observability.md).

## 6. 에러 처리

### 6-1. 외부에 내부 메시지 노출 금지

```python
# api/recommend.py
try:
    resp = await run_pipeline(req)
except Exception:
    logger.exception("recommend pipeline failed item_id=%s", req.item.id)
    raise HTTPException(status_code=502, detail="pipeline_failed") from None
```

`raise ... from None` 으로 chaining 끊고, detail은 **고정 문자열**만. 원인은 로그/Langfuse trace 에서 추적.

### 6-2. Provider 응답 검증

외부 응답 스키마는 **신뢰하지 말고** 명시적으로 검증:

```python
# providers/embedding.py
data = resp.json()
embedding = data.get("embedding")
if not isinstance(embedding, list) or not embedding:
    raise ValueError(f"Modal /embed unexpected response keys={list(data.keys())}")
return embedding
```

## 7. 인증 — X-Internal-Token

`/recommend` + `/health/ready` 는 `verify_internal_token` dependency 로 보호.

```python
@router.post("/recommend", dependencies=[Depends(verify_internal_token)])
```

Next.js 측이 `X-Internal-Token: <INTERNAL_API_TOKEN>` 헤더 첨부. 토큰 미설정 시(dev) 검증 스킵.

`/health` (liveness) 는 무인증 — 부울만 반환.

## 8. SSRF 가드

`RecommendRequest.image_url` 은 `field_validator` 로 `ALLOWED_IMAGE_HOSTS` 화이트리스트 검증. 운영에선 R2 public 도메인만 허용:

```bash
ALLOWED_IMAGE_HOSTS=pub-xxx.r2.dev,r2.cloudflarestorage.com
```

## 9. Lint / Format

```bash
uv run ruff check .          # lint
uv run ruff format .         # format (변경)
uv run ruff format --check . # CI 검증용 (변경 X)
```

`pyproject.toml`:
- `target-version = "py313"`
- `line-length = 120`
- `select = ["E", "F", "I", "N", "W", "UP"]`

## 10. 테스트

```bash
uv run pytest -q
```

`pytest-asyncio` `mode=auto` — `async def test_*` 가 자동 인식. fixtures: `tests/conftest.py` 의 `client` (httpx AsyncClient).

목 패턴 (Provider 클래스 메서드 패치):

```python
with patch("app.api.health.SupabaseProvider.check_connection",
           new_callable=AsyncMock, return_value=True):
    resp = await client.get("/health/ready", headers={"X-Internal-Token": "..."})
```

## 11. Docker

multi-stage uv build:

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

GHA에서 `linux/arm64` 네이티브 러너로 빌드 → ECR push.
