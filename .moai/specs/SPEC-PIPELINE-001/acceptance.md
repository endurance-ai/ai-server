---
id: SPEC-PIPELINE-001
type: acceptance
created: 2026-05-04
updated: 2026-05-04
---

# Acceptance Criteria — SPEC-PIPELINE-001

## HISTORY

- 2026-05-04: 초안 작성 (manager-spec).

---

## 1. Given-When-Then Scenarios

### Scenario A — 정상 보강 (status=ok)

**Given**
- `ENHANCE_QUERY_ENABLED=true`, `PIPELINE_PARALLEL_ENABLED=true`
- `req.item.search_query_ko = "베이지색 오버사이즈 니트 스웨터 가을 데일리룩"`
- `req.item.search_query = "beige oversized knit sweater for autumn daily look"`
- `req.item.subcategory = "knit"`, `req.item.brand = "uniqlo"`
- LLMProvider mock 이 `{"refined_ko": "베이지 오버사이즈 니트 스웨터", "refined_en": "beige oversized knit sweater"}` 반환

**When**
- `POST /recommend` 가 호출되어 파이프라인이 실행

**Then**
- `state.enhance_query_status == "ok"`
- `state.enhanced_query_ko == "베이지 오버사이즈 니트 스웨터"`
- `state.enhanced_query == "beige oversized knit sweater"`
- `search_step` 가 Supabase RPC 호출 시 `query_text="베이지 오버사이즈 니트 스웨터"` 사용 (mock 인자 검증)
- HTTP 응답 200 OK, `RecommendResponse` 정상 반환
- Langfuse trace 에 `pipeline.enhance_query` span 존재, `metadata.status="ok"`

---

### Scenario B — LLM 타임아웃 폴백 (status=fallback)

**Given**
- `ENHANCE_QUERY_ENABLED=true`, `ENHANCE_QUERY_TIMEOUT_MS=1500`
- LLMProvider mock 이 `await asyncio.sleep(2.0)` 으로 2초 지연 후 응답 (타임아웃 초과)
- 입력 쿼리는 Scenario A 와 동일

**When**
- `POST /recommend` 호출

**Then**
- `state.enhance_query_status == "fallback"`
- `state.enhanced_query_ko is None` 이고 `state.enhanced_query is None`
- `search_step` 가 `query_text="베이지색 오버사이즈 니트 스웨터 가을 데일리룩"` (raw search_query_ko) 으로 호출
- HTTP 응답 200 OK — **타임아웃이 추천 응답을 막지 않는다** [HARD]
- Langfuse trace 에 `metadata.fallback_reason="timeout"` 기록
- 구조적 warning 로그 1건 (level=WARNING, reason=timeout)

---

### Scenario C — Feature flag off (status=disabled)

**Given**
- `ENHANCE_QUERY_ENABLED=false`
- 입력 쿼리는 Scenario A 와 동일
- LLMProvider mock 의 호출 카운터 0 으로 시작

**When**
- `POST /recommend` 호출

**Then**
- `state.enhance_query_status == "disabled"`
- LLMProvider.chat() 호출 횟수 == 0 (mock 카운터로 검증)
- `search_step` 가 raw `search_query_ko` 로 호출
- HTTP 응답 200 OK
- Langfuse trace `metadata.status="disabled"`, `metadata.fallback_reason` 미설정

---

### Scenario D — JSON 파싱 실패 폴백

**Given**
- `ENHANCE_QUERY_ENABLED=true`
- LLMProvider mock 이 `"이건 JSON이 아닙니다 그냥 문장입니다"` 평문 반환
- regex 폴백도 매칭 실패

**When**
- `POST /recommend` 호출

**Then**
- `state.enhance_query_status == "fallback"`
- `search_step` 가 raw `search_query_ko` 로 호출
- HTTP 응답 200 OK
- Langfuse `metadata.fallback_reason="parse_error"`

---

### Scenario E — 길이 검증 실패 폴백

**Given**
- `ENHANCE_QUERY_ENABLED=true`
- LLMProvider mock 이 `{"refined_ko": "", "refined_en": "x"}` 반환 (refined_ko 빈 문자열)

**When**
- `POST /recommend` 호출

**Then**
- `state.enhance_query_status == "fallback"`
- `metadata.fallback_reason == "length_invalid"`
- search_step 은 raw 쿼리로 호출
- HTTP 200 OK

---

### Scenario F — 입력 쿼리 모두 비어있음 (status=skipped)

**Given**
- `ENHANCE_QUERY_ENABLED=true`
- `req.item.search_query == ""` 이고 `req.item.search_query_ko == ""`

**When**
- `POST /recommend` 호출

**Then**
- `state.enhance_query_status == "skipped"`
- LLMProvider 호출 횟수 == 0
- `search_step` 가 raw(빈) 쿼리로 호출 (sparse 채널 BM25 가 점수 0 으로 처리 — dense 채널만 동작)
- HTTP 응답 200 OK

---

### Scenario G — 병렬 실행 latency (PIPELINE_PARALLEL_ENABLED=true)

**Given**
- `PIPELINE_PARALLEL_ENABLED=true`
- embed_step mock latency = 800ms
- enhance_query_step mock latency = 600ms

**When**
- `POST /recommend` 호출 후 `state.latency_ms` 측정

**Then**
- `state.latency_ms["embed"]` ≈ 800ms (±100ms 허용)
- `state.latency_ms["enhance_query"]` ≈ 600ms (±100ms 허용)
- 두 단계 wall-clock 합산이 1400ms 이 아니라 max(embed, enhance) ≈ 800ms 근처여야 함 (병렬 검증)

---

### Scenario H — 병렬 실행 시 enhance 예외 격리

**Given**
- `PIPELINE_PARALLEL_ENABLED=true`
- enhance_query_step 내부에서 모든 폴백 처리 후에도 만약 예상 못한 예외 발생 (mock 으로 raise) — 단, 실제 구현은 top-level except 로 잡혀 status=fallback 반환되어야 함
- embed_step 은 정상 동작

**When**
- `POST /recommend` 호출

**Then**
- HTTP 200 OK (enhance 예외가 추천 응답을 abort 시키지 않음)
- `state.embedding` 정상 채워짐
- `state.enhance_query_status == "fallback"` 또는 logged warning

---

## 2. Edge Cases

| Edge case | Expected |
|-----------|----------|
| `search_query` 만 있고 `search_query_ko` 없음 | enhance 정상 동작, refined_ko 도 LLM 이 생성. 실패 시 raw search_query 만으로 search_step 진행 |
| `search_query_ko` 길이 200자 초과 | LLM 입력으로 그대로 전달 (절단하지 않음). 응답 검증에서 refined 길이만 체크 |
| LLMProvider 가 None (의존성 미주입) | enhance_query_step 시작 시 가드 → status=fallback, fallback_reason="provider_missing" |
| `attributes` 가 매우 큰 dict (1KB+) | 프롬프트에 그대로 직렬화. max_tokens=200 응답 한도라 출력에는 영향 없음 |
| LLM 응답이 정상 JSON 인데 추가 필드 포함 | refined_ko, refined_en 만 사용. 추가 필드 무시 |

## 3. Quality Gate Criteria

### Coverage [HARD]

- `app/pipeline/enhance_query.py` 라인 커버리지 ≥ 90%
- 폴백 매트릭스(plan §3.2) 의 모든 fallback_reason 분기 단위 테스트 1개 이상

### Tests [HARD]

- `tests/test_enhance_query.py` 모든 케이스 통과 (8개 시나리오)
- `tests/test_pipeline_with_enhance.py` 모든 케이스 통과 (3개 시나리오)
- 기존 `tests/test_health`, `tests/test_config` 회귀 없음

### Lint [HARD]

- `uv run ruff check .` 0 error
- `uv run ruff format --check .` clean

### Typing

- `enhance_query_status` 가 `Literal["ok", "fallback", "disabled", "skipped"]` 으로 타입 명세
- LLMProvider mock 이 실제 `LLMProvider.chat` 시그니처와 호환 (typing.Protocol 또는 동일 dataclass)

### Observability

- 로컬에서 `POST /recommend` 1회 실행 시 Langfuse(self-host) 에 `pipeline.enhance_query` span 도달 (수동 검증)
- `metadata.fallback_reason` 이 fallback 케이스에서만 설정됨

### Backward Compatibility [HARD]

- `ENHANCE_QUERY_ENABLED=false` (기본값) 상태에서 기존 추천 응답 형태(JSON 스키마, product_ids 순서 모두) 100% 동일
- 기존 호출자(portal/app) 의 코드 변경 0
- `RecommendResponse` 스키마 변경 없음

## 4. Definition of Done

- [ ] `app/pipeline/enhance_query.py` 작성 완료, `@observe` 적용
- [ ] `app/pipeline/state.py` 에 3개 필드 추가
- [ ] `app/pipeline/runner.py` 가 `PIPELINE_PARALLEL_ENABLED` 분기 + `asyncio.gather` 적용
- [ ] `app/pipeline/search.py` 가 status==ok 일 때 enhanced_query 사용
- [ ] `app/core/config.py` 에 환경변수 6종 추가
- [ ] unit + integration 테스트 모두 통과
- [ ] 모든 Quality Gate 통과 (Coverage / Tests / Lint / Typing / Observability)
- [ ] CLAUDE.md, docs/features/pipeline.md 업데이트
- [ ] MX 태그 (@MX:ANCHOR, @MX:WARN, @MX:NOTE) 적용
- [ ] `ENHANCE_QUERY_ENABLED=false` 기본값으로 머지 (안전 롤아웃)
- [ ] Langfuse 에서 trace 가 정상 생성되는 것을 1회 수동 확인

## 5. Manual Verification Checklist (배포 후)

- [ ] flag off 상태에서 추천 응답 latency 회귀 없음 (P95 비교)
- [ ] flag on 후 Langfuse 에서 `status=ok` vs `status=fallback` 비율 확인 (목표: ok ≥ 90%)
- [ ] sparse 채널 hit 분포 변화 확인 (검색 결과 다양성/관련성 정성 평가)
- [ ] 비용 대시보드: gpt-4o-mini 호출 빈도가 추천 트래픽 1:1 비율인지
