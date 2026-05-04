---
id: SPEC-PIPELINE-001
version: 0.1.0
status: draft
created: 2026-05-04
updated: 2026-05-04
author: hchsa77@gmail.com
priority: medium
issue_number: 4
---

# SPEC-PIPELINE-001: enhance_query LLM 단계 도입 (sparse 검색 쿼리 보강)

## HISTORY

- 2026-05-04: 초안 작성 (manager-spec).

---

## 1. Overview (개요)

portal-ai 추천 파이프라인에 **LLM 기반 쿼리 정제 단계(enhance_query)** 를 도입한다. portal/app(Vision)이 생성한 raw 검색 쿼리(`search_query`, `search_query_ko`)를 Supabase `search_products_v5` RPC 의 sparse 채널(pgroonga BM25)에 더 적합한 형태로 보강해 sparse 정확도를 끌어올리는 것이 목적이다.

현재 파이프라인은 `embed → search → diversify` 의 plain async state-machine 으로 동작하며, sparse 채널의 입력 품질이 raw 쿼리의 형태(불필요한 수식어, 비표준 토큰, 한·영 혼용)에 직접 의존하고 있다. dense 채널(이미지 임베딩, FashionSigLIP)은 본 SPEC 의 영향을 받지 않는다.

본 SPEC 은 **추천 파이프라인의 모든 실패 경로에서 안전하게 폴백** 하여 응답 가용성(SLO)을 절대 저하시키지 않는 것을 절대 원칙으로 한다.

## 2. Background (배경)

### 2.1 현재 파이프라인 구조

| 파일 | 책임 |
|------|------|
| `app/pipeline/runner.py` | sequential `await embed_step → search_step → diversify_step` |
| `app/pipeline/state.py` | `PipelineState` dataclass (request, embedding, raw_candidates, final_candidates, counts, latency_ms) |
| `app/pipeline/embed.py` | Modal `/embed` 호출 → state.embedding |
| `app/pipeline/search.py` | Supabase RPC `search_products_v5(query_embedding, query_text, ...)` |
| `app/pipeline/diversify.py` | 다양성 캡 + tolerance |
| `app/providers/llm.py` | LiteLLM 프록시 경유 async chat() — **현재 미사용** |

### 2.2 입력 출처

- `req.item.search_query` (영문 raw, GPT-4o-mini Vision 산출)
- `req.item.search_query_ko` (한글 raw, 동일 산출)
- `req.item.subcategory`, `req.item.brand`, `req.item.attributes` (메타)

### 2.3 문제

- pgroonga BM25 는 토큰 단위 정확도가 중요 — Vision 산출 raw 쿼리는 문장형/꾸밈말이 섞여 sparse hit 손실
- LLMProvider 가 이미 구현되어 있으나 파이프라인에 미연결
- 단일 LLM 실패가 전체 추천을 실패시키면 안 됨 → 강한 폴백이 필수

## 3. Constitution Alignment

- **기술 스택**: Python 3.x + FastAPI + Pydantic v2 + httpx (CLAUDE.md 명시)
- **LLM 호출 경로**: LiteLLM 프록시 경유 (`LITELLM_BASE_URL`) — 직접 OpenAI SDK 사용 금지
- **관측**: Langfuse self-host, `@observe` 데코레이터 의무
- **상태 머신 패턴**: plain async + state → state (LangGraph 보류 정책 유지)

## 4. Requirements (EARS)

### REQ-PIPELINE-001 (Ubiquitous)

The pipeline **shall** execute `enhance_query_step` immediately before `search_step` in every recommendation request lifecycle.

### REQ-PIPELINE-002 (Event-driven)

**When** `ENHANCE_QUERY_ENABLED=true` **and** the LLM call returns a valid response within `ENHANCE_QUERY_TIMEOUT_MS`, the `search_step` **shall** use the refined query (`state.enhanced_query_ko` for the `query_text` argument and `state.enhanced_query` for any English fallback) instead of the raw `req.item.search_query_ko` / `req.item.search_query`.

### REQ-PIPELINE-003 (Unwanted behavior)

**If** the LLM call experiences any of the following failure modes — timeout, HTTP 5xx, network error, empty response, JSON parse failure, length validation failure (refined string length < 1 or > 200 characters) — **then** the pipeline **shall** fall back to the original `req.item.search_query_ko` / `req.item.search_query`, set `state.enhance_query_status = "fallback"`, emit a Langfuse tag `fallback_reason=<reason>`, log a structured warning, and **shall not** raise an exception that aborts the request.

### REQ-PIPELINE-004 (State-driven)

**While** `ENHANCE_QUERY_ENABLED=false`, the `enhance_query_step` **shall not** invoke the LLM, **shall** set `state.enhance_query_status = "disabled"`, and **shall** pass through to `search_step` using the original raw query.

### REQ-PIPELINE-005 (Optional)

**Where** `PIPELINE_PARALLEL_ENABLED=true` (default), the pipeline **shall** execute `embed_step` and `enhance_query_step` concurrently via `asyncio.gather` so that the two independent I/O operations overlap.

## 5. Non-Functional Requirements

| 영역 | 목표 |
|------|------|
| Latency 영향 | enhance_query overhead ≤ `ENHANCE_QUERY_TIMEOUT_MS` (1500ms 기본). 병렬 실행 시 추가 지연 ≤ max(0, T_enhance − T_embed). |
| Availability | 전체 LLM 장애 상황에서도 추천 응답 SLO 영향 0 (폴백 100% 통과). |
| Observability | Langfuse trace 에 model, status, latency_ms, fallback_reason, original/refined 쿼리 모두 기록. |
| Cost | gpt-4o-mini @ ~200 tokens output → 요청당 $0.0001 미만 추정 (POC 수용 범위). |

## 6. Configuration (신규 환경변수)

`app/core/config.py` 에 다음 항목을 추가한다 (기본값은 안전 롤아웃 우선):

| Key | Type | Default | 설명 |
|-----|------|---------|------|
| `ENHANCE_QUERY_ENABLED` | bool | `False` | 단계 활성화 플래그 (운영 검증 후 true 전환) |
| `ENHANCE_QUERY_MODEL` | str | `"gpt-4o-mini"` | LiteLLM 라우팅 키 |
| `ENHANCE_QUERY_TIMEOUT_MS` | int | `1500` | LLM 호출 타임아웃 (ms) |
| `ENHANCE_QUERY_MAX_TOKENS` | int | `200` | 응답 토큰 상한 |
| `ENHANCE_QUERY_TEMPERATURE` | float | `0.2` | 안정성 우선 |
| `PIPELINE_PARALLEL_ENABLED` | bool | `True` | embed + enhance 병렬 실행 |

## 7. State Schema Changes

`app/pipeline/state.py` `PipelineState` 에 다음 3개 필드 추가:

| 필드 | 타입 | 의미 |
|------|------|------|
| `enhanced_query` | `str \| None` | 정제된 영문 쿼리 (status=ok 일 때만 채움) |
| `enhanced_query_ko` | `str \| None` | 정제된 한글 쿼리 (status=ok 일 때만 채움) |
| `enhance_query_status` | `Literal["ok", "fallback", "disabled", "skipped"]` | 단계 결과 상태 |

`status` 의미:
- `ok`: LLM 호출 성공, 검증 통과, 정제 쿼리 채택
- `fallback`: LLM 호출 시도했으나 실패 — 원본 사용
- `disabled`: feature flag off — LLM 미호출
- `skipped`: 입력 쿼리 자체가 비어있어 단계 건너뜀

## 8. Files to Modify

| Marker | Path | 변경 내용 |
|--------|------|----------|
| [NEW] | `app/pipeline/enhance_query.py` | `enhance_query_step(state) -> PipelineState` 신규. LLMProvider 호출, JSON 파싱, 검증, 폴백, `@observe(name="pipeline.enhance_query")`. |
| [MODIFY] | `app/pipeline/state.py` | `enhanced_query`, `enhanced_query_ko`, `enhance_query_status` 필드 추가. |
| [MODIFY] | `app/pipeline/runner.py` | `enhance_query_step` 등록. `PIPELINE_PARALLEL_ENABLED` 분기로 `asyncio.gather(embed_step, enhance_query_step)` 또는 sequential. |
| [MODIFY] | `app/pipeline/search.py` | `state.enhanced_query_ko` / `state.enhanced_query` 가 있고 `status=="ok"` 이면 우선 사용. 아니면 `req.item.search_query_ko` / `search_query`. |
| [MODIFY] | `app/core/config.py` | `ENHANCE_QUERY_*`, `PIPELINE_PARALLEL_ENABLED` 환경변수 6개 추가. |
| [NEW] | `tests/test_enhance_query.py` | unit: 정상 / 타임아웃 / 5xx / 빈응답 / 파싱실패 / disabled / skipped (LLMProvider mock). |
| [NEW] | `tests/test_pipeline_with_enhance.py` | integration: stub LLM + stub Supabase, full pipeline assertion (ok / fallback / disabled). |
| [MODIFY] | `CLAUDE.md` | 핵심 파일 표에 `app/pipeline/enhance_query.py` 1행 추가. |
| [MODIFY] | `docs/features/pipeline.md` | embed → **enhance_query** → search → diversify 다이어그램 갱신. |

## 9. MX Tags (계획)

- `@MX:ANCHOR` — `enhance_query_step()` 함수 진입점 (`fan_in ≥ 1`: runner.py, 향후 다른 파이프라인 변형에서도 진입 가능)
- `@MX:WARN` — `runner.py` 의 `asyncio.gather` 블록 (예외 분기, 한쪽 실패 시 다른 쪽 처리)
- `@MX:NOTE` — fallback 정책 설명 (검증 임계값 1~200자, response_format json_object 시도→정규식 폴백)

## 10. Exclusions (What NOT to Build)

- **영구 캐싱 / Redis 도입**: POC 범위 외. 향후 별도 SPEC.
- **dense(이미지) 임베딩 변경**: Modal FashionSigLIP 호출 그대로 유지.
- **portal/app Vision 호출 변경**: raw 쿼리 생성 책임은 portal/app 에 그대로 둠.
- **다국어 자동 언어 감지**: 현재 `search_query` (en) + `search_query_ko` (ko) 양쪽을 받는 구조 유지.
- **rerank 스텝 추가**: 별도 SPEC 으로 분리. 본 SPEC 은 sparse 입력 품질 개선만 다룸.
- **프롬프트 자체의 동적 최적화 / 자가 학습**: 정적 프롬프트 1종으로 시작.
- **다중 LLM 폴백 체인**: gpt-4o-mini 1개 모델만 호출, 실패 시 즉시 raw 폴백.

## 11. Risks

| Risk | Mitigation |
|------|------------|
| LLM 응답이 검색 품질을 오히려 악화 | feature flag(default off) + Langfuse 에서 ok/fallback 별 검색 결과 분포 모니터 가능 |
| 병렬 실행 시 한쪽 예외가 전체 파이프라인 abort | `asyncio.gather(..., return_exceptions=True)` + enhance 측 항상 폴백 보장 |
| LiteLLM 프록시 자체 장애 | 타임아웃 1500ms 로 가드, 폴백 status="fallback" |
| Pydantic 검증 실패 (refined_ko/en 누락) | JSON 파싱 후 키 존재 + 길이 검증, 실패 시 폴백 |
| 비용 폭증 | max_tokens=200, 호출당 평균 $0.0001 미만, 예산 초과 시 flag off 즉시 가능 |

## 12. References

- `docs/features/pipeline.md` — 현행 state-machine 설명
- `docs/PATTERNS.md` — 코드 컨벤션 (plain async, Pydantic v2)
- `app/providers/llm.py` — 미사용 LLMProvider (본 SPEC 에서 첫 사용처)
- portal/app `RecommendItem` 스키마 — `search_query` / `search_query_ko` 출처
