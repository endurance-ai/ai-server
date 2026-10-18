# search_products_v6 RPC 계약

> SPEC-ARCH-AI-001 REQ-AI-006 + SPEC-SEARCH-V6-001. `app/infrastructure/repositories/search_rpc_contract.py` 의 Pydantic 모델이 정규 소스.
> v5 계약 (`search_products_v5` + pgroonga + `product_search_text`) 은 DB에서 DROP됨 — 이 문서는 v6 계약만 다룬다.

## 목적

AI 서버가 `search_products_v6` RPC 응답 행을 diversify 에 넘기기 전에 형상을 검증한다.
계약 드리프트가 발생하면 조용한 오채점 대신 구조화 에러로 표면화된다.

## 입력 파라미터 (`SearchRepository.build_params`)

| 파라미터 | 타입 | 기본값 | 비고 |
|---------|------|--------|------|
| `query_embedding` | `vector(768)` string | 필수 | pgvector 형식 `[f:.7f, ...]` (768 원소). 이미지 경로: `EmbedProvider.embed_image_url`. 텍스트 경로: `EmbedProvider.embed_text` (Modal `POST /embed/text`, 동일 FashionSigLIP L2 공간) |
| `p_style_node_id` | `text` | `NULL` | style-node 필터 — AI 서버는 항상 NULL (style-node 개념 미사용) |
| `p_category` | `text` | 필수 | `to_canonical_family(item.category)` 정규화 결과 — 반드시 20개 canonical token 중 하나 (`"other"` 포함). `"other"` 이면 family gate 스킵 (graceful degrade) |
| `p_subcategory` | `text` | `NULL` | 항상 NULL — `products.subcategory` 가 DB 전체 100% NULL (narrowing 무의미) |
| `p_brand_names` | `text[]` | `NULL` | 브랜드 필터 (없으면 NULL) |
| `p_limit` | `integer` | `50` | 반환 후보 수 (`SEARCH_DEFAULT_K`) |
| `p_platform` | `text` | `NULL` | 판매처 exact 필터. null이면 전체 검색. 편집샵 범위에서는 category fallback도 이 필터 안에서 유지 |
| `p_min_price` | `integer` | `NULL` | 가격 하한(원, 경계 포함). 값이 있을 때만 키를 보낸다 (2026-10-18) |
| `p_max_price` | `integer` | `NULL` | 가격 상한(원, 경계 포함). 값이 있을 때만 키를 보낸다 (2026-10-18) |

**v5에서 제거된 파라미터**: `query_text`, `gender_filter`, `subcategory_filter`, `price_min`, `price_max`, `tags_filter`, `k`, `rrf_k`. 가격은 2026-10-18에 `p_min_price` / `p_max_price` 이름으로 다시 들어왔다.

**가격 파라미터 (`search_products_v6`, `search_products_hybrid_v1` 공통)**

- 출처: `RecommendRequest.price_filter`. 에이전트 경로에서는 LLM 인자 `min_price` / `max_price`, 상한이 없으면 모바일 필터 슬라이더 `req_price_max`.
- 모든 rung · CTE 에서 다른 하드 필터와 같은 자리에서 거르고 완화 재시도에서도 풀지 않는다.
- 가격 조건이 있을 때만 함수 안에서 `hnsw.iterative_scan = relaxed_order` 를 켠다(트랜잭션 로컬). 끄면 HNSW 가 `ef_search`(40) 개만 꺼낸 뒤 가격으로 걸러 후보가 크게 줄어든다.
- 가격 키가 없을 때는 키를 보내지 않는다. SQL 이 배포되기 전 옛 함수에 새 키를 보내면 PostgREST 가 함수를 찾지 못해(PGRST202) 가격 없는 검색까지 실패하기 때문이다.

RPC 이름 리터럴 `"search_products_v6"` 는 `search_repository.py` 의 `_RPC_NAME` 상수에 단일 소스로 존재 (REQ-AI-002).

## 응답 행 형상 (`SearchRpcRowContract`)

| 필드 | 타입 | 필수 여부 | 비고 |
|------|------|----------|------|
| `id` | `int \| str` | **필수** | bigint. PostgREST가 int 또는 str 반환 모두 가능. 누락 시 `RpcContractError`. runner 가 `str(row["id"])` 적용 |
| `brand` | `str \| None` | 선택 | 누락 허용 — runner 기본값 `""` |
| `name` | `str \| None` | 선택 | |
| `price` | `int \| float \| None` | 선택 | |
| `image_url` | `str \| None` | 선택 | |
| `product_url` | `str \| None` | 선택 | |
| `platform` | `str \| None` | 선택 | 다양성 캡 기준 |
| `subcategory` | `str \| None` | 선택 | 실제값 항상 NULL |
| `distance` | `float \| None` | 선택 | cosine distance ASC (낮을수록 유사). 누락 허용 — runner: `score = 1.0 - distance` (absent → score 0.0) |
| `degraded` | `bool \| None` | 선택 | style-node filter drop → category-only fallback 여부. 관측 전용 |
| 미지 컬럼 | any | 허용 | `extra="allow"` — 전방 호환 |

**v5에서 제거된 응답 컬럼**: `score`, `dense_rank`, `sparse_rank`, `dense_score`, `sparse_score`. runner 는 `score = 1.0 - distance` 로 변환 후 downstream에 전달. `dense_rank`/`sparse_rank` 는 항상 `None` 으로 설정 (스키마 필드는 유지, 값 없음).

계약은 **허용 범위 기준** (permissive): 검증 통과 행은 **원본 dict 그대로** downstream 에 전달 (coercion 없음).

## 드리프트 동작 (REQ-AI-006)

1. `validate_rpc_rows(rows)` 가 첫 번째 위반 행에서 `RpcContractError(row_index, detail)` raise.
2. `search_service.search_service()` 가 `RpcContractError` 를 캐치:
   ```
   logger.error("[STEP 4.5][search] RPC contract drift -- failing open to empty result (row_index=%s exc=%s)", ...)
   ```
3. `state.raw_candidates = []` 로 fail-open → pipeline 은 빈 결과를 반환 (502 없음).
4. Langfuse 트레이스에 에러 스팬 기록.

## canonical family gate 로그

`search_service` 는 RPC 호출 전에 family gate 상태를 로깅:

```
[STEP 4.5][search] category raw='Outer' → canonical='outerwear' family_gate=active
[STEP 4.5][search] category raw=None → canonical='other' family_gate=skipped(other)
```

`other` → gate 스킵은 정상 동작 (cosine-only degrade, NOT broken).

## 위치

| 파일 | 내용 |
|------|------|
| `app/infrastructure/repositories/category_family.py` | `CANONICAL_FAMILIES` (20 tokens) + `to_canonical_family()` — family gate 단일 소스 |
| `app/infrastructure/repositories/search_rpc_contract.py` | `SearchRpcRowContract`, `RpcContractError`, `validate_rpc_rows` |
| `app/infrastructure/repositories/search_repository.py` | `SearchRepository.build_params` (파라미터 빌드) + `SearchRepository.search` (RPC 호출 + 계약 검증 트리거) |
| `app/services/search_service.py` | `RpcContractError` 캐치 → 구조화 ERROR 로그 + fail-open |
| `tests/test_arch_ai_001/test_rpc_contract_drift.py` | Net 4 — v6 드리프트 케이스 (id 누락, distance 비숫자, degraded 허용, empty rows, happy path) |
| `tests/test_category_family.py` | `to_canonical_family()` unit suite (20 canonical tokens + 7 Vision-enum aliases + edge cases) |
