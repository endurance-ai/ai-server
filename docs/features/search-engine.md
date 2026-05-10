# 검색 엔진 v5

> Supabase pgvector(HNSW) + pgroonga(BM25) + RRF. AI 서버는 RPC 호출 + 다양성 캡만 담당.

## 책임 경계 (B 옵션)

| 레이어 | 책임 |
|--------|------|
| **Postgres RPC** (`search_products_v5`) | dense (HNSW) + sparse (pgroonga) + RRF → top-K |
| **AI 서버 Python** (`pipeline/diversify.py`) | 다양성 캡 + tolerance + 최종 정렬 |

근거:
- DB는 인덱스를 잘 활용하는 영역(벡터/풀텍스트)만 담당
- 비즈니스 로직(다양성/리랭크/A/B)은 변경 빈도가 높으므로 Python 측에 둠 — 핫리로드 + 평가 스크립트 옆에서 작성

## RPC 인터페이스

`kikoai/app/supabase/migrations/030_search_products_v5.sql`:

```sql
search_products_v5(
  query_embedding vector(768),
  query_text text DEFAULT NULL,
  brand_filter text[] DEFAULT NULL,
  gender_filter text[] DEFAULT NULL,
  subcategory_filter text DEFAULT NULL,
  price_min integer DEFAULT NULL,
  price_max integer DEFAULT NULL,
  tags_filter text[] DEFAULT NULL,
  k integer DEFAULT 50,
  rrf_k integer DEFAULT 60
) RETURNS TABLE (
  id uuid,
  brand text, name text, price integer,
  image_url text, product_url text, platform text,
  subcategory text, color text, material text, style_node text,
  gender text[], tags text[],
  dense_rank integer, sparse_rank integer,
  dense_score double precision, sparse_score double precision,
  score double precision   -- RRF score
)
```

## 알고리즘

### Hard filter (in-RPC)

```
in_stock = true
brand_filter (활성 시)
gender_filter (활성 시)
subcategory_filter (활성 시)
price_min/max (활성 시)
tags_filter (활성 시)
```

### Dense (HNSW)

```sql
SELECT id, 1 - (embedding <=> query_embedding) AS sim,
       row_number() OVER (...) AS r
FROM products
WHERE embedding IS NOT NULL AND <hard_filter>
ORDER BY embedding <=> query_embedding ASC
LIMIT k * 4
```

- `vector_ip_ops` HNSW (FashionSigLIP은 L2-normalized → cos ≈ inner product)
- `m=16, ef_construction=200` (마이그레이션 027)
- 런타임 튜닝: `SET LOCAL hnsw.ef_search = N` (기본 40)

### Sparse (pgroonga)

```sql
SELECT id, pgroonga_score(p.tableoid, p.ctid) AS sim,
       row_number() OVER (...) AS r
FROM products p
WHERE product_search_text(p) &@~ query_text
  AND <hard_filter>
ORDER BY pgroonga_score(...) DESC
LIMIT k * 4
```

- `product_search_text(p)` = `brand || ' ' || name || ' ' || description || ' ' || material || ' ' || color`
- pgroonga 한국어 토크나이저 (027 인덱스 정의와 동일 표현식)

> **주의**: `pgroonga_score` 는 실제 테이블의 시스템 컬럼(`tableoid`, `ctid`) 필요. CTE 의 `SELECT p.*` 결과로는 동작하지 않음 — 두 CTE 모두 `products` 직접 참조.

### RRF (Reciprocal Rank Fusion)

```
score = 1/(rrf_k + dense_rank) + 1/(rrf_k + sparse_rank)
```

- `rrf_k = 60` (기본)
- dense 또는 sparse 한 쪽만 매칭되면 그 항만 기여
- 최종 ORDER BY `score DESC`

## 다양성 캡 (Python 측)

`app/pipeline/diversify.py`:

```python
target = req.final_limit or _tolerance_to_target_count(req.tolerance)
brand_cap    = SEARCH_BRAND_CAP * 3 if req.brand_filter else SEARCH_BRAND_CAP   # 2 또는 6
platform_cap = SEARCH_PLATFORM_CAP                                              # 3

for c in raw_candidates:                # RRF 순서 유지
    if seen_brand[c.brand] >= brand_cap: continue
    if seen_platform[c.platform] >= platform_cap: continue
    out.append(c)
    if len(out) >= target: break
```

| tolerance | target |
|-----------|--------|
| 0.0 | 10 (tight) |
| 0.5 | 15 (medium, 기본) |
| 1.0 | 20 (loose) |

## 응답 모양

```json
{
  "id": "uuid",
  "brand": "Acme",
  "name": "Cropped Hoodie",
  "price": 89000,
  "imageUrl": "https://...",
  "productUrl": "https://...",
  "platform": "shopamomento",
  "subcategory": "hoodie",
  "score": 0.0317,
  "denseRank": 3,
  "sparseRank": 7
}
```

## v4 (Next.js) 폴백

AI 서버 5xx/timeout 시 Next.js 의 `/api/find/search` 가 기존 v4 검색(`/api/search-products`) 을 in-process 호출.

| | v4 (Next.js, 폴백) | v5 (이 서버) |
|---|---|---|
| 알고리즘 | enum 가중합 (13차원) | dense + sparse + RRF |
| 데이터 의존 | `product_ai_analysis` INNER JOIN | `products.embedding` (FashionSigLIP) |
| 다양성 | brand 2 / platform 3 (동일) | brand 2 / platform 3 (동일) |

v4는 점진적으로 폐기 예정 (v5 검증 후).

## 평가 / 디버깅

- **검색 디버거**: kikoai/app `/admin/search-debugger` (v4 기반, v5 토글은 미작성 — 백로그)
- **로그**: `search_quality_logs` 테이블 (v4 만 기록 중. v5 로깅은 백로그)
- **trace**: Langfuse `recommend_pipeline` → `pipeline.search` span 의 input/output 전체 노출

## 관련 마이그레이션

| 파일 | 내용 |
|------|------|
| `kikoai/app/supabase/migrations/027_product_embeddings_and_pgroonga.sql` | embedding 컬럼 + HNSW + pgroonga 인덱스 + bulk_update RPC + coverage 뷰 |
| `kikoai/app/supabase/migrations/030_search_products_v5.sql` | 본 RPC (`search_products_v5`) + `product_search_text` 헬퍼 |
