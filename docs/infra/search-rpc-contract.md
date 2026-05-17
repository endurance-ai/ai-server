# search_products_v5 RPC 계약

> SPEC-ARCH-AI-001 REQ-AI-006. `app/infrastructure/repositories/search_rpc_contract.py` 의 Pydantic 모델이 정규 소스.

## 목적

AI 서버가 `search_products_v5` RPC 응답 행을 scoring/diversify 에 넘기기 전에 형상을 검증한다.
계약 드리프트가 발생하면 조용한 오채점 대신 구조화 에러로 표면화된다.

## 입력 파라미터 (`SearchRepository.build_params`)

| 파라미터 | 타입 | 기본값 | 비고 |
|---------|------|--------|------|
| `query_embedding` | `vector(768)` string | 필수 | pgvector 형식 `[f:.7f, ...]` (768 원소) |
| `query_text` | `text` | 필수 | 3-tier 선택: `enhanced_query_ko` → `enhanced_query` → `search_query_ko or search_query` |
| `brand_filter` | `text[]` | `None` | 브랜드 필터 (없으면 NULL) |
| `gender_filter` | `text[]` | `None` | 성별 필터 |
| `subcategory_filter` | `text` | `None` | 서브카테고리 필터 |
| `price_min` | `integer` | `None` | 최저가 필터 |
| `price_max` | `integer` | `None` | 최고가 필터 |
| `tags_filter` | `text[]` | `None` | 태그 필터 |
| `k` | `integer` | `50` | 반환 후보 수 |
| `rrf_k` | `integer` | `60` | RRF 상수 |

RPC 이름 리터럴 `"search_products_v5"` 는 `search_repository.py` 의 `_RPC_NAME` 상수에 단일 소스로 존재 (REQ-AI-002).

## 응답 행 형상 (`SearchRpcRowContract`)

| 필드 | 타입 | 필수 여부 | 비고 |
|------|------|----------|------|
| `id` | `str \| int` | **필수** | 누락 시 `RpcContractError`. runner가 `str(row["id"])` 적용 |
| `brand` | `str \| None` | 선택 | 누락 허용 — runner 기본값 `""` |
| `name` | `str \| None` | 선택 | |
| `price` | `int \| float \| None` | 선택 | |
| `image_url` | `str \| None` | 선택 | |
| `product_url` | `str \| None` | 선택 | |
| `platform` | `str \| None` | 선택 | 다양성 캡 기준 |
| `subcategory` | `str \| None` | 선택 | |
| `score` | `int \| float \| None` | 선택 | 누락 허용 — runner 기본값 `0.0` |
| `dense_rank` | `int \| None` | 선택 | |
| `sparse_rank` | `int \| None` | 선택 | |
| 미지 컬럼 | any | 허용 | `extra="allow"` — 전방 호환 |

계약은 **허용 범위 기준** (permissive): PR6 이전 runner 가 이미 받아들이던 모든 행 형상을 수용한다.
검증 통과 행은 **원본 dict 그대로** downstream 에 전달 (coercion 없음 — happy path byte-identical, REQ-AI-007).

## 드리프트 동작 (REQ-AI-006)

1. `validate_rpc_rows(rows)` 가 첫 번째 위반 행에서 `RpcContractError(row_index, detail)` raise.
2. `search_service.search_service()` 가 `RpcContractError` 를 캐치:
   ```
   logger.error("[STEP 4.5][search] RPC contract drift -- failing open to empty result (row_index=%s exc=%s)", ...)
   ```
3. `state.raw_candidates = []` 로 fail-open → pipeline 은 빈 결과를 반환 (502 없음).
4. Langfuse 트레이스에 에러 스팬 기록.

**[HARD]** 드리프트 브랜치가 추가된 것 외에 외부 행동은 변경 없음 (REQ-AI-007).

## 위치

| 파일 | 내용 |
|------|------|
| `app/infrastructure/repositories/search_rpc_contract.py` | `SearchRpcRowContract`, `RpcContractError`, `validate_rpc_rows` |
| `app/infrastructure/repositories/search_repository.py` | `SearchRepository.build_params` (파라미터 빌드) + `SearchRepository.search` (RPC 호출 + 계약 검증 트리거) |
| `app/services/search_service.py` | `RpcContractError` 캐치 → 구조화 ERROR 로그 + fail-open |
| `tests/test_arch_ai_001/test_rpc_contract_drift.py` | Net 4 — 드리프트 5개 케이스 (id 누락, 미지 컬럼, score 누락 허용, empty rows, happy path) |

## 관련 마이그레이션

`search_products_v5` 함수 정의: `kikoai/app/supabase/migrations/030_search_products_v5.sql`.
