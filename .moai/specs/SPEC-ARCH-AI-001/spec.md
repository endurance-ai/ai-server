---
id: SPEC-ARCH-AI-001
version: 0.1.1
status: draft
created: 2026-05-16
updated: 2026-05-16
author: MoAI orchestrator (manager-spec)
priority: high
issue_number: 0
---

# SPEC-ARCH-AI-001 — AI 서비스 best-practice FastAPI 레이어링

> **리포 경계**: 이 SPEC 과 그 구현 코드는 외부 리포 `endurance-ai/ai-server` (`https://github.com/endurance-ai/ai-server.git`, 로컬 `/Users/hansangho/Desktop/kikoai/ai`) 에서 추적된다 — kiko.ai-app 리포와 별개. 경로는 모두 ai-server 리포 루트 기준 (예: `app/pipeline/search.py` = ai-server 리포의 `app/pipeline/search.py`, `docs/infra/` = ai-server 리포의 `docs/infra/`, `tests/` = ai-server 리포의 `tests/`). 이 리포 내부의 FastAPI 패키지 `app/` 는 kiko.ai-app 리포와 무관한 별개 패키지다.

## HISTORY

- 2026-05-16 v0.1.1: app 리포에서 ai-server 리포로 이전 (`endurance-ai/ai-server` 가 별도 리포임을 확인 — 코드+SPEC 동일 리포에서 추적). 모든 경로를 ai-repo-root-relative 로 정정 (`ai/` prefix 제거; ai 리포 내부 FastAPI 패키지 `app/` 는 그대로 유지). 내용 변경 없음.
- 2026-05-16 v0.1.0: 초안. stack-internal 재설계 4-SPEC 분해 중 2번 (실행 순서 2번). 언어 불변 (Python FastAPI 유지). 감사 결과 — `app/` 에 `pipeline/` (search/diversify/embed/enhance_query/runner) + `graphs/nodes/` + `providers/` (db_pool/database/embedding/llm) + `channels/` (session_pg/taste_profile_pg) 존재. 비즈니스 로직이 pipeline step / graph node 에 인라인, RPC 명(`search_products_v5`) 이 `app/pipeline/search.py:54` 에 하드코딩, `app/providers/db_pool.py` 가 모듈-글로벌 상태 확인.

## Overview

ai-server 리포는 메인 플로우 v5 의 본체다. (app 리포의) `find/search` → `POST {AI_SERVER_URL}/recommend` → `app/pipeline/runner.py:run_pipeline` → `search_products_v5` RPC → 스코어링/다양성(`app/pipeline/diversify.py`). 현재 비즈니스 로직이 `app/pipeline/` step 함수와 `app/graphs/nodes/` 에 인라인 분산되어 있고, DB 접근(`SupabaseProvider.rpc`)이 RPC 명 하드코딩 + 모듈-글로벌 풀(`app/providers/db_pool.py`)에 결합되어 있다. 이는 best-practice FastAPI 레이어링(서비스/리포지토리/DI/도메인 분리) 으로 추출되어야 한다.

이것은 재작성이 아니라 ~10-15% 코드 이동(extraction)이다. **사용자 가시 동작 변화 0 — `/recommend` 응답(itemId/results/counts/latencyMs)은 재설계 전후 동일 입력에 대해 동등해야 한다 (HARD).** 검색 결과 품질(스코어링/다양성 산식)은 1 bit 도 바꾸지 않는다.

[HARD] **ai-server 는 post-RPC 스코어링/다양성의 단일 진실원천(single source of truth)으로 남는다.** `DiversifyService` (현 `app/pipeline/diversify.py` 추출본)가 SPEC-SEARCH-UNIFY-001 의 v5 엔진이 가리키는 대상이다. v4 로직을 v5 로 병합하지 않는다. 사용자가 진행 중인 v6 작업을 막거나 리팩터하지 않는다 — v6 는 같은 서비스 경계 뒤로 드롭인된다.

## Goals (EARS-format requirements)

### REQ-AI-001 (Ubiquitous) — Service Layer Extraction

The AI service **shall** expose business logic through a `app/services/` layer (`search_service`, `embed_service`, `diversify_service`, `database_service`), extracting logic currently inlined in `app/pipeline/` steps and `app/graphs/nodes/`, so pipeline steps and graph nodes become thin orchestrators that call services.

### REQ-AI-002 (Ubiquitous) — Repository Abstraction

The AI service **shall** access the search RPC through `app/infrastructure/repositories/search_repository.py` (`SearchRepository`) which encapsulates the `search_products_v5` RPC call, decoupling business code from the hardcoded RPC name currently at `app/pipeline/search.py:54`.

[HARD] RPC 명/파라미터 매핑은 `SearchRepository` 한 곳에만 존재한다. v6 가 다른 RPC/엔드포인트를 제공할 때 리포지토리 교체로 흡수 (SPEC-SEARCH-UNIFY-001 v6 seam 의 ai 측 앵커).

### REQ-AI-003 (Event-driven) — DI Container Replaces Module-Global State

**When** a request handler or service requires a DB pool, settings, or embedding client, the AI service **shall** resolve it via a `app/core/di.py` FastAPI `Depends` container, replacing module-global state in `app/providers/db_pool.py` and the settings singleton.

### REQ-AI-004 (Ubiquitous) — Memory Infrastructure Relocation

The AI service **shall** locate session and taste-profile persistence under `app/infrastructure/memory/`, relocating `app/channels/session.py`, `app/channels/session_pg.py`, `app/channels/taste_profile.py`, `app/channels/taste_profile_pg.py` without changing their runtime behavior.

### REQ-AI-005 (Ubiquitous) — Domain Model / DTO Separation

The AI service **shall** separate internal domain models (`app/domain/`, `app/core/types.py`) from Pydantic request/response DTOs (`app/models/`), so transport schemas and business types evolve independently.

### REQ-AI-006 (Event-driven) — RPC Contract Validation

**When** the `search_products_v5` RPC returns rows, the AI service **shall** validate the response shape against a documented RPC contract (Pydantic model) before scoring/diversify, surfacing contract drift as a structured error rather than silent malformed scoring.

### REQ-AI-007 (Unwanted) — No Scoring/Diversity Behavior Change

The AI service **shall not** alter the post-RPC scoring weights, RRF parameters, or diversity cap logic (`app/pipeline/diversify.py` `brand_cap`/`platform_cap`, `tolerance→target_count`) during this re-layering. Extraction preserves arithmetic byte-identically.

## Acceptance Criteria

상세 Given/When/Then 시나리오는 `acceptance.md` 참고. 필수 게이트:

- **[HARD] 사용자 가시 동작 & 화면 불변**: (app 리포) `/` 메인 플로우(IG → Vision → v5 검색)의 결과 카드/순서/화면 변경 0. 동일 `RecommendRequest` 입력에 대해 `/recommend` 응답의 `results` 순서·점수·counts 가 재설계 전후 동등.
- **[HARD] Characterization-tests-precede-refactor 게이트**: 추출 착수 전, 핵심 경로 characterization tests 작성·통과 필수 — (1) `run_pipeline` 엔드투엔드(고정 embedding fixture → search → diversify → response 스냅샷), (2) `diversify_step` 스코어링/캡 산식(입력 후보 → 출력 순서 스냅샷), (3) `SupabaseProvider.rpc("search_products_v5", ...)` 파라미터 매핑 스냅샷. ai-server 는 현재 테스트가 일부 존재(`tests/`)하나 핵심 경로 회귀 그물은 본 SPEC 가 보강한다.
- **타깃 폴더 레이아웃** (감사에서 도출한 구체 디렉터리명, ai-server 리포 루트 기준):
  - `app/services/` — `search_service.py` / `embed_service.py` / `diversify_service.py` / `database_service.py`
  - `app/infrastructure/repositories/` — `search_repository.py` (`search_products_v5` RPC 래핑)
  - `app/infrastructure/memory/` — `channels/session*.py` + `taste_profile*.py` 이전
  - `app/core/di.py` — FastAPI `Depends` 컨테이너 (`app/providers/db_pool.py` 모듈-글로벌 대체)
  - `app/core/types.py` + `app/domain/` — 도메인 모델 (vs `app/models/` Pydantic DTO)
  - 문서화된 RPC 계약 — `app/infrastructure/repositories/search_rpc_contract.py` (Pydantic) + `docs/infra/` RPC 계약 문서
- **롤백 전략**: 레이어별 독립 PR (services → repositories → di → memory relocation → domain split). 각 추출은 기존 모듈을 thin re-export shim 으로 남겨 import 경로 호환 유지 → 회귀 시 shim 으로 즉시 복원, shim 제거는 모든 추출 안정 후 별도 정리 PR. DI 전환은 `app/providers/db_pool.py` 를 di 컨테이너 위임 어댑터로 유지 (구 호출부 무중단).

## Doc Sync

> 이 SPEC 의 코드는 ai-server 리포에 있으나, CLAUDE.md 필수 동기화 3종 doc (`docs/ARCHITECTURE.md` / `docs/features/main-flow.md` / `docs/features/search-engine.md`) 은 **kiko.ai-app 리포**에 있다 (ai-server 리포 내부 doc `docs/` 와는 별개). ai SPEC 완료 시 app 리포 측 3종 doc 갱신은 **app 리포 PR 로 별도 수행**한다 (cross-repo 동기화). app 측 3종은 ai 내부 레이어링이 메인 플로우/검색 토폴로지에 미치는 영향만 반영.

- (app 리포) `docs/ARCHITECTURE.md` — AI 서버 토폴로지: `app/pipeline/` 인라인 → `app/services/`+`app/infrastructure/` 레이어 구조로 갱신, RPC 계약 위치 명시. **이 SPEC 완료 시 app 리포 PR 로 갱신 필수.**
- (app 리포) `docs/features/main-flow.md` — `/api/find/search` → `/recommend` 흐름 설명에서 ai 내부 서비스 경계 갱신 (외부 동작 불변이므로 토폴로지 주석 수준). app 리포 PR 시 변경 여부 diff 와 cross-check 후 명시.
- (app 리포) `docs/features/search-engine.md` — v5 가 단일 스코어링/다양성 진실원천임을 `DiversifyService` 경계로 명시 (SPEC-SEARCH-UNIFY-001 의 v5 타깃 앵커). **이 SPEC 완료 시 app 리포 PR 로 갱신 필수.**
- (ai-server 리포) `docs/infra/` — RPC 계약 문서 (`search_products_v5` 입출력 Pydantic 계약) 신설/갱신. 이 SPEC 완료 시 ai-server 리포 PR 로 갱신.

## What NOT to Build (Exclusions / NOT in scope)

- 언어 마이그레이션 (Go/Rust) — 전면 금지. Python FastAPI 유지.
- 스코어링/다양성 산식 변경 (가중치, RRF k, 캡) — 추출만, 산식 동결 (REQ-AI-007).
- v4 로직의 v5 병합 — 명시적 금지. v4 는 SPEC-SEARCH-UNIFY-001 의 thin fallback 으로만 존재.
- v6 작업 차단/리팩터 — v6 는 같은 `SearchRepository`/`DiversifyService` 경계 뒤로 드롭인. 본 SPEC 는 v6 seam 의 ai 측 경계를 **준비**만 한다.
- v5 임베딩 풀배치 실행 (81k 인코딩) — 별도 인프라 작업 (ai-server 리포 `scripts/aws/embed_products.py`).
- `app/graphs/` LangGraph 토폴로지 재설계 — node 의 비즈니스 로직 추출만, 그래프 라우팅/상태머신 구조 변경 아님.
- 텔레그램/Pinterest 채널 신규 기능 — `app/channels/` 는 memory 이전(REQ-AI-004) 외 변경 없음.

## Dependency Ordering & Parallelism

- **실행 순서**: 4-SPEC 중 **2번**. crawler 다음. (다른 SPEC 위치: SPEC-ARCH-CRAWLER-001 = `endurance-ai/crawler` 리포; SPEC-ARCH-APP-001 / SPEC-SEARCH-UNIFY-001 = kiko.ai-app 리포.)
- **병렬 가능**: SPEC-ARCH-CRAWLER-001 과 병렬 가능 (독립 리포). SPEC-ARCH-APP-001 과는 순차 권장 — app 의 `domains/search-v5-client` 가 ai 의 안정된 `/recommend` 계약을 타깃하므로 ai 레이어링이 먼저 안정화되면 app 작업이 흔들리지 않음.
- **선행 의존**: 없음 (독립). **후행 의존**: SPEC-SEARCH-UNIFY-001 이 본 SPEC 의 `SearchRepository`/`DiversifyService` 안정 계약에 의존.

## Cross-References

> 아래 SPEC 들의 위치: SPEC-SEARCH-UNIFY-001 / SPEC-ARCH-APP-001 = kiko.ai-app 리포 `.moai/specs/`; SPEC-ARCH-CRAWLER-001 = `endurance-ai/crawler` 리포 `.moai/specs/` (cross-repo 참조).

- SPEC-SEARCH-UNIFY-001 (app 리포): **강결합**. v5 엔진 = ai `/recommend` (이 SPEC 의 `search_service`+`SearchRepository`+`DiversifyService`). v6 seam 의 ai 측 앵커 = `SearchRepository` 교체점 (REQ-AI-002). SEARCH-UNIFY 의 port 계약(req/resp shape)은 ai `RecommendRequest`/`RecommendResponse` DTO 와 정합해야 함 (REQ-AI-005 도메인/DTO 분리가 이를 가능케 함).
- SPEC-ARCH-APP-001 (app 리포): app `src/domains/search-v5-client/` 가 이 SPEC 가 안정화한 `/recommend` 계약을 호출. 계약 변경 0 (외부 동작 불변) 이므로 app 측 클라이언트 코드 무변경.
- SPEC-ARCH-CRAWLER-001 (crawler 리포): 병렬 독립. 동일 재설계 철학(서비스/전략 추출) 정렬.
