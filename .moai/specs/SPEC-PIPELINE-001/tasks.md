---
id: SPEC-PIPELINE-001
type: tasks
created: 2026-05-04
---

# Task Decomposition

SPEC: SPEC-PIPELINE-001 — enhance_query LLM 단계 도입

| Task ID | Description | Requirement | Dependencies | Planned Files | Status |
|---------|-------------|-------------|--------------|---------------|--------|
| T-001 | Settings 추가 (`ENHANCE_QUERY_*` 5종 + `PIPELINE_PARALLEL_ENABLED`) | REQ-PIPELINE-002, 004, 005 | - | app/core/config.py | pending |
| T-002 | PipelineState 필드 확장 (`enhanced_query`, `enhanced_query_ko`, `enhance_query_status` Literal) | REQ-PIPELINE-001~004 | T-001 | app/pipeline/state.py | pending |
| T-003 | `enhance_query_step` 신규 구현 (LLM 호출 + 전수 폴백 + @observe) | REQ-PIPELINE-001, 002, 003, 004 | T-001, T-002 | app/pipeline/enhance_query.py (NEW) | pending |
| T-004 | runner.py 결선 (parallel/sequential 분기, asyncio.gather return_exceptions) | REQ-PIPELINE-001, 005 | T-003 | app/pipeline/runner.py | pending |
| T-005 | search.py 에서 status==ok 일 때 enhanced query 우선 사용 | REQ-PIPELINE-002 | T-002, T-003 | app/pipeline/search.py | pending |
| T-006 | unit 테스트 8 시나리오 (ok/timeout/5xx/empty/parse/length/disabled/skipped) | AC-A, B, C, D, E, F | T-003 | tests/test_enhance_query.py (NEW) | pending |
| T-007 | integration 테스트 (search_step mock 인자 검증, 병렬 latency, 예외 격리) | AC-A, B, C, G, H | T-004, T-005 | tests/test_pipeline_with_enhance.py (NEW) | pending |
| T-008 | MX 태그 적용 (ANCHOR enhance_query_step, WARN gather 블록, NOTE 폴백 정책) | non-functional | T-003, T-004 | app/pipeline/enhance_query.py, runner.py | pending |
| T-009 | docs 업데이트 (CLAUDE.md 핵심 파일 표, docs/features/pipeline.md 다이어그램) | non-functional | T-003 | CLAUDE.md, docs/features/pipeline.md | pending |
