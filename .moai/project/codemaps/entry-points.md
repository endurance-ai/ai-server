# portal-ai 진입점 카탈로그

## ASGI 앱

| 항목 | 값 |
|------|-----|
| ASGI 객체 | `app.main:app` |
| 로컬 실행 명령 | `uv run uvicorn app.main:app --reload --port 8000` |
| 운영 실행 명령 | `uvicorn app.main:app --host 0.0.0.0 --port 8000` (Dockerfile ENTRYPOINT) |
| 기본 응답 클래스 | `ORJSONResponse` (orjson 기반 고성능 직렬화) |

## Lifespan 훅

파일: `app/main.py` — `lifespan` asynccontextmanager

| 이벤트 | 동작 |
|--------|------|
| Startup | `SupabaseProvider.get_client()` 호출 → Supabase async 클라이언트 워밍업 (첫 요청 race condition 방지) |
| Shutdown | `SupabaseProvider.close()`, `EmbedProvider.close()`, `LLMProvider.close()` 순차 호출 |

Supabase URL과 SERVICE_ROLE_KEY가 모두 설정된 경우에만 startup 워밍업이 실행된다.

## HTTP 엔드포인트

### POST /recommend

| 항목 | 내용 |
|------|------|
| 파일 | `app/api/recommend.py` |
| 인증 | `X-Internal-Token` 헤더 (`verify_internal_token` dependency) |
| 요청 바디 | `RecommendRequest` (JSON, camelCase alias) |
| 응답 | `RecommendResponse` (JSON, camelCase serialization_alias), `ORJSONResponse` |
| 에러 | 파이프라인 예외 → `HTTP 502 {"detail": "pipeline_failed"}` (내부 스택 비노출) |
| 에러 | 토큰 불일치 → `HTTP 401 {"detail": "unauthorized"}` |
| 에러 | 요청 검증 실패 (SSRF 등) → `HTTP 422 Unprocessable Entity` |

요청 필드 요약:

| 필드 (alias) | 타입 | 필수 | 설명 |
|--------------|------|------|------|
| `item` | `AnalyzedItem` | Y | Vision 분석 결과 아이템 |
| `imageUrl` | `str` | Y | R2 이미지 URL (SSRF 가드 적용) |
| `brandFilter` | `list[str] \| null` | N | 브랜드 화이트리스트 |
| `gender` | `str \| null` | N | 성별 필터 |
| `styleNode` | `StyleNode \| null` | N | 스타일 분류 노드 |
| `moodTags` | `list[str] \| null` | N | 무드 태그 |
| `priceFilter` | `PriceFilter \| null` | N | 가격 범위 필터 |
| `tolerance` | `float` | N (기본 0.5) | 다양성 조절 (0.0~1.0) |
| `finalLimit` | `int \| null` | N | 최종 결과 수 override (1~50) |

### GET /health

| 항목 | 내용 |
|------|------|
| 파일 | `app/api/health.py` |
| 인증 | 없음 (liveness — 인프라 LB/Docker healthcheck 전용) |
| 응답 | `{"status": "ok", "version": "..."}` |
| 상태 코드 | 항상 200 |

### GET /health/ready

| 항목 | 내용 |
|------|------|
| 파일 | `app/api/health.py` |
| 인증 | `X-Internal-Token` 헤더 필요 |
| 동작 | Supabase(`check_connection`), Modal(`/health` GET), LiteLLM(`check_connection`) 순차 점검 |
| 응답 (정상) | `{"status": "ok", "supabase": "connected", "modal_embed": "connected", "litellm": "connected", "version": "..."}`, 200 |
| 응답 (이상) | `{"status": "degraded", ...}`, 503 |

## CLI / 스크립트

| 스크립트 | 위치 | 용도 | 필요 의존 그룹 |
|----------|------|------|---------------|
| `embed_batch_local.py` | `scripts/` | 로컬에서 이미지 URL 배치 임베딩 처리 | optional `embed` 그룹 (torch, open-clip-torch, Pillow, tqdm) |

## Docker

| 항목 | 내용 |
|------|------|
| `Dockerfile` ENTRYPOINT | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| `docker-compose.yml` 서비스명 | ai (portal-ai 서버) |
| 로컬 스택 실행 | `docker compose up -d` |
| 운영 이미지 | embed 옵션 그룹(torch 등) 미포함 — 경량 이미지 |
