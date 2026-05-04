---
id: SPEC-PIPELINE-001
version: 0.1.0
status: draft
created: 2026-05-04
updated: 2026-05-04
author: hchsa77@gmail.com
priority: medium
issue_number: 4
type: spec-compact
---

# SPEC-PIPELINE-001 (Compact): enhance_query LLM 단계 도입

## HISTORY

- 2026-05-04: 초안 작성 (manager-spec).

---

## Requirements (EARS)

- **REQ-PIPELINE-001 (Ubiquitous)**: The pipeline **shall** execute `enhance_query_step` immediately before `search_step` in every recommendation request lifecycle.
- **REQ-PIPELINE-002 (Event-driven)**: **When** `ENHANCE_QUERY_ENABLED=true` **and** the LLM call returns a valid response within `ENHANCE_QUERY_TIMEOUT_MS`, the `search_step` **shall** use `state.enhanced_query_ko` / `state.enhanced_query` instead of `req.item.search_query_ko` / `req.item.search_query`.
- **REQ-PIPELINE-003 (Unwanted behavior)**: **If** the LLM call experiences timeout, HTTP 5xx, network error, empty response, JSON parse failure, or length validation failure (1~200 chars), **then** the pipeline **shall** fall back to the original raw query, set `state.enhance_query_status="fallback"`, emit Langfuse tag `fallback_reason=<reason>`, and **shall not** raise an exception that aborts the request.
- **REQ-PIPELINE-004 (State-driven)**: **While** `ENHANCE_QUERY_ENABLED=false`, `enhance_query_step` **shall not** invoke the LLM, **shall** set `state.enhance_query_status="disabled"`, and **shall** pass through to `search_step` using the raw query.
- **REQ-PIPELINE-005 (Optional)**: **Where** `PIPELINE_PARALLEL_ENABLED=true` (default), the pipeline **shall** execute `embed_step` and `enhance_query_step` concurrently via `asyncio.gather`.

## Acceptance (요약, Given→When→Then)

| ID | Given | When | Then |
|----|-------|------|------|
| A — 정상 보강 | flag=on, LLM mock 정상 JSON | `POST /recommend` | `status=ok`, search_step 이 refined query 사용, 200 OK |
| B — 타임아웃 폴백 | flag=on, LLM 2s 지연(>1.5s) | `POST /recommend` | `status=fallback`, `fallback_reason=timeout`, raw 쿼리 사용, 200 OK |
| C — flag off | `ENHANCE_QUERY_ENABLED=false` | `POST /recommend` | `status=disabled`, LLM 호출 0, raw 쿼리 사용, 200 OK |
| D — 파싱 실패 | LLM 평문 응답 + regex 실패 | `POST /recommend` | `status=fallback`, `fallback_reason=parse_error`, 200 OK |
| E — 길이 위반 | LLM `refined_ko=""` | `POST /recommend` | `status=fallback`, `fallback_reason=length_invalid`, 200 OK |
| F — skipped | search_query 양쪽 모두 빈 문자열 | `POST /recommend` | `status=skipped`, LLM 호출 0, 200 OK |
| G — 병렬 latency | parallel=on, embed=800ms, enhance=600ms | 측정 | 총 wall-clock ≈ max(800, 600), not sum |
| H — 예외 격리 | parallel=on, enhance 내부 예외 | `POST /recommend` | embed 결과 보존, 200 OK |

### Quality Gates [HARD]

- coverage `app/pipeline/enhance_query.py` ≥ 90%
- 폴백 매트릭스의 모든 `fallback_reason` 분기 1개 이상 단위 테스트
- `ruff check` / `ruff format --check` 0 error
- `ENHANCE_QUERY_ENABLED=false` 기본값으로 backward compatibility 100%

## Files to Modify

| Marker | Path | 변경 내용 |
|--------|------|----------|
| [NEW] | `app/pipeline/enhance_query.py` | `enhance_query_step()` 신규, LLMProvider 호출 + 전수 폴백 + `@observe(name="pipeline.enhance_query")` |
| [MODIFY] | `app/pipeline/state.py` | `enhanced_query`, `enhanced_query_ko`, `enhance_query_status: Literal["ok","fallback","disabled","skipped"]` 추가 |
| [MODIFY] | `app/pipeline/runner.py` | `PIPELINE_PARALLEL_ENABLED` 분기, `asyncio.gather(embed, enhance, return_exceptions=True)` |
| [MODIFY] | `app/pipeline/search.py` | `status=="ok"` 이면 enhanced query 사용, 아니면 raw |
| [MODIFY] | `app/core/config.py` | `ENHANCE_QUERY_ENABLED` (False), `ENHANCE_QUERY_MODEL` ("gpt-4o-mini"), `ENHANCE_QUERY_TIMEOUT_MS` (1500), `ENHANCE_QUERY_MAX_TOKENS` (200), `ENHANCE_QUERY_TEMPERATURE` (0.2), `PIPELINE_PARALLEL_ENABLED` (True) |
| [NEW] | `tests/test_enhance_query.py` | unit (8 시나리오: ok / timeout / 5xx / empty / parse_error / length_invalid / disabled / skipped) |
| [NEW] | `tests/test_pipeline_with_enhance.py` | integration (ok / fallback / disabled), search_step mock 인자 검증 |
| [MODIFY] | `CLAUDE.md` | 핵심 파일 표 1행 추가 |
| [MODIFY] | `docs/features/pipeline.md` | 다이어그램에 enhance_query 단계 반영 |

## Exclusions (What NOT to Build)

- 영구 캐싱 / Redis 도입 (별도 SPEC)
- dense(이미지) 임베딩 변경 (Modal FashionSigLIP 그대로)
- portal/app Vision 호출 변경 (raw 쿼리 생성 책임은 portal/app)
- 다국어 자동 언어 감지 (ko/en 둘 다 받는 구조 유지)
- rerank 스텝 추가 (별도 SPEC)
- 프롬프트 자가 학습 / 동적 최적화 (정적 프롬프트 1종)
- 다중 LLM 폴백 체인 (단일 모델, 실패 시 즉시 raw)

## Configuration Defaults

| Key | Default |
|-----|---------|
| `ENHANCE_QUERY_ENABLED` | `False` |
| `ENHANCE_QUERY_MODEL` | `"gpt-4o-mini"` |
| `ENHANCE_QUERY_TIMEOUT_MS` | `1500` |
| `ENHANCE_QUERY_MAX_TOKENS` | `200` |
| `ENHANCE_QUERY_TEMPERATURE` | `0.2` |
| `PIPELINE_PARALLEL_ENABLED` | `True` |
