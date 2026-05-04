---
id: SPEC-PIPELINE-001
type: plan
created: 2026-05-04
updated: 2026-05-04
---

# Implementation Plan — SPEC-PIPELINE-001

## HISTORY

- 2026-05-04: 초안 작성 (manager-spec).

---

## 1. 구현 전략 요약

본 SPEC 은 brownfield modification 으로, 기존 plain async state-machine 의 **search_step 직전** 에 `enhance_query_step` 을 삽입한다. 핵심 설계 결정은 다음과 같다.

1. **병렬 실행** (default): `asyncio.gather(embed_step, enhance_query_step)` — embed 와 enhance 는 입력 의존성이 없어 동시 실행 가능. 추정 latency 절감 200~500ms.
2. **항상 폴백** [HARD]: enhance_query_step 은 어떤 예외도 호출자에 전파하지 않는다. 내부에서 try/except 전수 → status 와 원본 쿼리로 항상 정상 PipelineState 반환.
3. **state.enhance_query_status 단일 디스패치 키**: search_step 은 status==ok 만 신뢰하고 정제 쿼리 사용. 그 외(fallback/disabled/skipped) 는 모두 raw 쿼리로 동작.

## 2. Milestones (priority-based)

### Milestone M1 — Configuration & State (Priority: High)

선행 조건. 다른 단계 모두가 이 스키마/설정에 의존.

- `app/core/config.py` 에 `ENHANCE_QUERY_*` (5종) + `PIPELINE_PARALLEL_ENABLED` 추가
- `app/pipeline/state.py` 에 `enhanced_query`, `enhanced_query_ko`, `enhance_query_status` 추가
- `Literal["ok", "fallback", "disabled", "skipped"]` 타입 정의
- pyproject.toml / requirements 변경 없음 (httpx, pydantic 기존 의존)

### Milestone M2 — enhance_query_step Implementation (Priority: High)

핵심 로직. 모든 폴백 분기를 단일 함수에서 처리.

- `app/pipeline/enhance_query.py` 신규
- 입력 검증 → status="skipped" 처리 (search_query, search_query_ko 모두 비어있는 경우)
- feature flag check → status="disabled" 처리
- LLMProvider.chat() 호출, asyncio.wait_for 로 타임아웃 가드
- JSON 파싱 (response_format json_object 시도 → 실패 시 regex 폴백)
- 검증: refined_ko, refined_en 키 존재 + 길이 1~200자
- 모든 실패 → state.enhance_query_status="fallback" + Langfuse tag fallback_reason
- `@observe(name="pipeline.enhance_query")` 적용

### Milestone M3 — Pipeline Integration (Priority: High)

runner + search 가 새 단계를 인식하도록 결선.

- `app/pipeline/runner.py` 에서 `PIPELINE_PARALLEL_ENABLED` 분기
  - true: `await asyncio.gather(embed_step(state), enhance_query_step(state), return_exceptions=True)`
  - false: sequential (`await embed_step → await enhance_query_step`)
  - gather 결과에서 예외는 enhance 쪽이라면 폴백, embed 쪽이면 기존 에러 전파
- `app/pipeline/search.py` 에서 query_text 결정 로직:
  - status=="ok" 이고 enhanced_query_ko 비어있지 않음 → enhanced_query_ko 사용
  - 그 외 → req.item.search_query_ko (없으면 search_query)
- 영문 fallback 도 동일 규칙 (enhanced_query → search_query)

### Milestone M4 — Tests (Priority: High)

폴백 경로 100% 커버하지 않으면 본 SPEC 은 머지 불가 [HARD].

- `tests/test_enhance_query.py` (unit, LLMProvider mock):
  - 정상 응답 → status=ok, enhanced_query 채움
  - asyncio.TimeoutError → status=fallback, fallback_reason=timeout
  - httpx 5xx (mock) → status=fallback, fallback_reason=http_5xx
  - 빈 응답 → status=fallback, fallback_reason=empty
  - JSON 파싱 실패 → status=fallback, fallback_reason=parse_error
  - 길이 검증 실패 (refined_ko="") → status=fallback, fallback_reason=length_invalid
  - feature flag off → status=disabled, LLM 미호출 (mock 호출 횟수 0 검증)
  - 입력 쿼리 모두 빈 문자열 → status=skipped
- `tests/test_pipeline_with_enhance.py` (integration, stub LLM + stub Supabase):
  - 정상 → search_step 이 enhanced_query_ko 로 호출됨 (mock 인자 검증)
  - 폴백 → search_step 이 raw search_query_ko 로 호출됨
  - disabled → 동일 raw 사용, LLM 호출 0
  - 병렬 실행 시 enhance 예외에도 embed 결과는 보존되어 추천 응답 200 OK

### Milestone M5 — Docs & MX Tags (Priority: Medium)

- `CLAUDE.md` 핵심 파일 표 1행 추가 (`app/pipeline/enhance_query.py`)
- `docs/features/pipeline.md` 다이어그램에 enhance_query 단계 추가
- `@MX:ANCHOR` (enhance_query_step), `@MX:WARN` (asyncio.gather 블록), `@MX:NOTE` (fallback 정책) 적용

### Milestone M6 — Rollout & Observability (Priority: Low)

- 운영 배포 시 `ENHANCE_QUERY_ENABLED=false` 로 코드만 먼저 머지
- Langfuse 대시보드에 `pipeline.enhance_query` trace 도달 확인 후
- shadow 운영(별도 호출 검증) 또는 점진적 트래픽으로 true 전환

## 3. Technical Approach

### 3.1 Prompt 후보 (M2 에서 확정)

```
SYSTEM:
You normalize Korean and English fashion search queries for BM25/pgroonga sparse retrieval.
Output JSON only with this exact shape: {"refined_ko": "<korean>", "refined_en": "<english>"}.
Rules:
- Keep core nouns (item type, color, material, silhouette).
- Drop filler words and decorative adjectives.
- Preserve brand and category tokens if present in the input.
- Do not invent attributes not in the input.
- Each refined string: 1 to 200 characters. No commentary.

USER:
raw_ko: "{search_query_ko}"
raw_en: "{search_query}"
subcategory: "{subcategory}"
brand: "{brand}"
attributes: {attributes_json}
```

`response_format={"type": "json_object"}` 우선 시도. LiteLLM 라우팅 모델이 미지원이면 평문 + 정규식(`r"\{[^{}]*\"refined_ko\"[^{}]*\}"`) 으로 폴백.

### 3.2 폴백 분기 매트릭스

| Failure mode | Detection | fallback_reason |
|--------------|-----------|-----------------|
| flag off | `settings.ENHANCE_QUERY_ENABLED is False` | (no fallback — disabled) |
| 입력 비어있음 | not search_query and not search_query_ko | (no fallback — skipped) |
| timeout | `asyncio.TimeoutError` | `timeout` |
| http 5xx | `httpx.HTTPStatusError` (5xx) | `http_5xx` |
| http 4xx | `httpx.HTTPStatusError` (4xx) | `http_4xx` |
| network | `httpx.RequestError` | `network` |
| 빈 응답 | `not response or not response.strip()` | `empty` |
| 파싱 실패 | `json.JSONDecodeError` + regex 도 실패 | `parse_error` |
| 키 누락 | `"refined_ko" not in parsed or "refined_en" not in parsed` | `missing_keys` |
| 길이 위반 | `len(refined) < 1 or len(refined) > 200` | `length_invalid` |
| 기타 예외 | `Exception` (top-level except) | `unknown` |

### 3.3 병렬 실행 패턴

```
# pseudo (in plan only, not implementation)
results = await asyncio.gather(
    embed_step(state),
    enhance_query_step(state),
    return_exceptions=True,
)
# results[0] is embed result or exception → embed 실패는 기존대로 raise
# results[1] is enhance result (enhance_query_step 자체가 예외 비전파 설계라 정상 PipelineState 반환 보장)
```

embed_step 과 enhance_query_step 둘 다 동일 PipelineState 인스턴스를 받지만 서로 다른 필드를 갱신하므로 race condition 없음:
- embed_step: `state.embedding`, `state.latency_ms["embed"]`
- enhance_query_step: `state.enhanced_query`, `state.enhanced_query_ko`, `state.enhance_query_status`, `state.latency_ms["enhance_query"]`

### 3.4 Observability 명세

`@observe(name="pipeline.enhance_query")` 데코레이터가 자동 trace 생성. 추가로 함수 내부에서 다음 메타를 set:

| Key | Value |
|-----|-------|
| input.original_query_ko | req.item.search_query_ko |
| input.original_query_en | req.item.search_query |
| output.refined_query_ko | state.enhanced_query_ko or null |
| output.refined_query_en | state.enhanced_query or null |
| metadata.model | settings.ENHANCE_QUERY_MODEL |
| metadata.status | state.enhance_query_status |
| metadata.latency_ms | state.latency_ms["enhance_query"] |
| metadata.fallback_reason | (only when status=fallback) |

## 4. Dependencies

| 의존 | 상태 |
|------|------|
| `app/providers/llm.py` LLMProvider | 기존 구현, 변경 없음 (사용처만 추가) |
| LiteLLM 프록시 (`LITELLM_BASE_URL`) | 운영 배포되어 있어야 함 (이미 인프라 존재) |
| Langfuse self-host | 기존 운영 (변경 없음) |
| Supabase RPC `search_products_v5` | 시그니처 변경 없음 |
| Modal /embed | 변경 없음 |

## 5. Out-of-Scope (다시 강조)

본 plan 은 SPEC §10 의 Exclusions 를 그대로 승계한다. 특히:
- rerank 단계 추가 금지 (별도 SPEC)
- 캐싱 추가 금지
- portal/app 책임 영역 변경 금지

## 6. Open Questions (annotation 단계에서 확정)

1. **병렬 실행 default 가 true 가 적절한가?** — 단순성 우선이면 false 시작 후 운영 검증 후 true 전환도 가능. (SPEC 초안: true)
2. **fallback 시에도 latency_ms["enhance_query"] 기록?** — 현재 plan: 기록 (실패까지 걸린 시간 자체가 운영 지표). 사용자 확정 필요.
3. **LiteLLM `response_format` 미지원 시 모델 변경?** — gpt-4o-mini 는 지원하나 라우팅 키에 따라 다름. 미지원 시 regex 폴백으로 충분한지, 아니면 모델 강제 변경할지 결정.
4. **status=skipped 도 fallback 으로 통합?** — 현재 plan 은 분리. observability 측면에선 분리가 명확하나 search_step 동작은 동일. 사용자 확정 필요.
