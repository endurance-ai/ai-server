# 검색 엔진 v6

> dev-app Postgres pgvector(HNSW) embedding-first. AI 서버는 RPC 호출 + 다양성 캡만 담당.
> v5 (dense HNSW + sparse pgroonga + RRF) → v6 마이그레이션: SPEC-SEARCH-V6-001.

## 책임 경계

| 레이어 | 책임 |
|--------|------|
| **Postgres RPC** (`search_products_v6`) | embedding cosine distance ASC → top-K. FILTER2 canonical family gate. `degraded` flag (style-node filter drop → category-only fallback) |
| **AI 서버 Python** (`services/diversify_service.py`, thin shim `pipeline/diversify.py`) | 다양성 캡 + tolerance + 최종 정렬 |

근거:
- DB는 인덱스를 잘 활용하는 영역(벡터)만 담당
- 비즈니스 로직(다양성/리랭크/family normalization)은 Python 측 — 핫리로드 + 평가 스크립트 옆에서 작성

## RPC 인터페이스 (v6)

```sql
search_products_v6(
  query_embedding   vector(768),           -- FashionSigLIP L2-normalized, required
  p_style_node_id   text    DEFAULT NULL,  -- style-node filter (always NULL from AI server)
  p_category        text    DEFAULT NULL,  -- canonical family token (20-token set, or "other")
  p_subcategory     text    DEFAULT NULL,  -- always NULL (products.subcategory 100% NULL)
  p_brand_names     text[]  DEFAULT NULL,  -- optional brand filter
  p_limit           integer DEFAULT 50     -- top-K
) RETURNS TABLE (
  id          bigint,          -- products.id (bigint; PostgREST may return int or str)
  brand       text,
  name        text,
  price       integer,
  image_url   text,
  product_url text,
  platform    text,
  subcategory text,
  distance    double precision, -- cosine distance ASC (lower = more similar)
  degraded    boolean           -- true = style-node filter dropped → category-only fallback
)
```

> **v5 → v6 변경 요약**: `query_text`/`gender_filter`/`subcategory_filter`/`price_min`/`price_max`/`tags_filter`/`k`/`rrf_k` 파라미터 제거. 응답 컬럼 `score`/`dense_rank`/`sparse_rank`/`dense_score`/`sparse_score` 제거 → `distance`/`degraded` 추가. `id` uuid → bigint.

## 알고리즘

### v6 검색 (embedding-first)

```
query_embedding → cosine distance (HNSW pgvector)
→ ORDER BY distance ASC → LIMIT p_limit
```

- `vector_ip_ops` HNSW (FashionSigLIP L2-normalized → cosine ≈ inner product)
- v5의 sparse(pgroonga) + RRF 완전 제거
- `product_search_text` 헬퍼 함수 드롭됨

### FILTER2 — canonical family gate

`p_category` 가 `CANONICAL_FAMILIES` 의 20개 토큰 중 하나 (예: `"outerwear"`, `"tops"`) 이면 family gate 활성. `"other"` 이거나 빈값이면 gate 스킵 (cosine-only degrade — 의도된 동작, NOT broken).

클라이언트 정규화는 `app/infrastructure/repositories/category_family.py:to_canonical_family()` 가 단일 소스. Vision 7-enum(`Outer/Top/Bottom/Shoes/Bag/Dress/Accessories`) → 정규 토큰 매핑 포함.

### text query path (v6)

텍스트 전용 턴: `EmbedProvider.embed_text(text_query)` → Modal `POST /embed/text` → 768-dim 벡터 (동일 FashionSigLIP L2 공간 — cross-modal cosine 유효). v5의 zero-dense + pgroonga 트릭 및 `_suppress_zero_dense_noise` stopgap 완전 제거.

**SPEC-GENDER-PIN-001 gender resolution (260522)** — `search_products.dispatch` 에서 임베딩 전 실행:

1. `_query_gender(text_query)` — text_query 내 `men`/`women`/`unisex` whole-word 탐지 (per-request explicit gender)
2. 없으면 `_lookup_profile_gender(ctx)` — `TasteProfile.gender` 핀 조회 (크로스세션)
3. 없고 이미지 없으면 → `_send_gender_card` 로 성별 카드 전송 + `pending_gender.set_pending` → `awaiting_gender` 에러 반환 (이 turn 종료)
4. `clarify:gender:*` 콜백 → `ingest._handle_gender_pick` — gender 를 `TasteProfile.gender` 에 핀 + pending 팝 후 gender-appended query 로 `run_text_only_search` 재실행 (migration 0008 `ai.user_taste_profile.gender TEXT` 필요)

이미지 포함 턴은 gender 없으면 `unisex` 자동 추가 (카드 블로킹 없음).

## 다양성 캡 (Python 측)

`app/services/diversify_service.py` (thin shim: `app/pipeline/diversify.py`):

```python
target = req.final_limit or _tolerance_to_target_count(req.tolerance)
brand_cap    = SEARCH_BRAND_CAP * 3 if req.brand_filter else SEARCH_BRAND_CAP   # 2 또는 6
platform_cap = SEARCH_PLATFORM_CAP                                              # 3

for c in raw_candidates:                # distance ASC 순서 유지
    if pid in seen_ids: continue        # product_id 레벨 dedup
    if content_key in seen_content: continue  # (brand, name_norm, price) 컨텐츠 레벨 dedup (260522)
    if seen_brand[c.brand] >= brand_cap: continue
    if seen_platform[c.platform] >= platform_cap: continue
    out.append(c)
    if len(out) >= target: break
```

> **컨텐츠 레벨 dedup (260522)**: 동일 상품이 다른 `product_id` 로 중복 등록된 경우 id-only 가드는 통과함. `(brand, name_norm, price)` 키로 추가 필터링 — `brand` 또는 `name` 이 비어있는 항목은 content key 없이 통과 (graceful fallback).

| tolerance | target |
|-----------|--------|
| 0.0 | 10 (tight) |
| 0.5 | 15 (medium, 기본) |
| 1.0 | 20 (loose) |

## 응답 모양 (v6)

```json
{
  "id": "12345678901",
  "brand": "Acme",
  "name": "Cropped Hoodie",
  "price": 89000,
  "imageUrl": "https://...",
  "productUrl": "https://...",
  "platform": "shopamomento",
  "subcategory": null,
  "score": 0.8317,
  "denseRank": null,
  "sparseRank": null
}
```

> `score = 1.0 - distance` (runner 변환 — higher=better downstream 시맨틱 유지). `denseRank`/`sparseRank` 는 항상 `null` (v6에서 제거됨).

## v4 (Next.js) 폴백

AI 서버 5xx/timeout 시 Next.js 의 `/api/find/search` 가 기존 v4 검색(`/api/search-products`) 을 in-process 호출.

| | v4 (Next.js, 폴백) | v6 (이 서버) |
|---|---|---|
| 알고리즘 | enum 가중합 (13차원) | embedding cosine (FashionSigLIP) |
| 데이터 의존 | 별도 분석 테이블 | `products.embedding` (FashionSigLIP) |
| 다양성 | brand 2 / platform 3 (동일) | brand 2 / platform 3 (동일) |

## 평가 / 디버깅

- **로그**: `[STEP 4.5][search]` — `category raw→canonical family_gate` 라인으로 family gate 활성 여부 확인
- **로그**: `[STEP 4.6][search]` — `distance_dist min/median/max` + `degraded_count`
- **trace**: Langfuse `recommend_pipeline` → `pipeline.search` span 의 input/output 전체 노출

## 관련 파일

| 파일 | 내용 |
|------|------|
| `app/infrastructure/repositories/category_family.py` | `CANONICAL_FAMILIES` (20 tokens) + `to_canonical_family()` — family gate 단일 소스 |
| `app/infrastructure/repositories/search_repository.py` | `_RPC_NAME = "search_products_v6"` + `build_params` (6-key) |
| `app/infrastructure/repositories/search_rpc_contract.py` | v6 row contract (`distance`+`degraded`) |
| `app/providers/embedding.py` | `embed_image_url` + `embed_text` (v6 text path). 260522: cache/Modal 각 타이밍 로그 |
| `app/agents/tools/search_products.py` | gender resolution (`_query_gender`, `_lookup_profile_gender`, `_send_gender_card`), `pipeline_exc_detail` 헬퍼, per-step 타이밍 |
| `app/agents/pending_gender.py` | gender 카드 pending 스토어 (SPEC-GENDER-PIN-001) |
| `app/agents/last_query.py` | 크로스턴 product query 스토어 (refine 드리프트 방지) |
| `app/services/diversify_service.py` | 브랜드/플랫폼 캡 + content-level dedup (260522) |
| `migrations/versions/0008_add_taste_gender.py` | `ai.user_taste_profile.gender TEXT` 추가 (SPEC-GENDER-PIN-001) |
| `infra/search-rpc-contract.md` | RPC 계약 상세 + drift 동작 |
