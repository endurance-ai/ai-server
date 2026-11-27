# `product_features` — VLM 산출물 계약

상태: **WAITING ON VLM OWNER** (아래 §3 세 항목 합의 필요)
범위: VLM 배치가 `public.product_features` 에 쓰는 값의 계약. 배치 구현은 VLM 담당 소유,
소비 측(검색/큐레이션/PDP)은 이 문서 기준으로 이미 전환 완료.
작성 2026-07-30. 실측은 모두 dev DB 직접 조회.

---

## 1. 왜 계약이 필요해졌나

**color·gender 의 단일 출처가 크롤러에서 `product_features` 로 이관됐다.**

크롤러가 뽑던 `products.color` 는 VLM `primary_color` 와 일치율이 **54.8%**
(71,775 / 131,058) 였다. 옵션 select·스와치·상품명 폴백을 아무리 쌓아도 사이트마다
색상 표기가 달라 한계였고, 그 오염된 값이 `search_products_v6` 의 **모든 rung 하드
필터**로 쓰이고 있었다. 그래서 크롤러의 색상·성별 추출을 전면 제거했다.

이관 후 소비처는 세 곳이다.

| 소비처 | 읽는 값 | 동작 |
|---|---|---|
| `search_products_v6` (3 rung + 2 count 프리체크) | `feature_metadata->>'primary_color'` | 색상은 **완화 없는 정밀 필터**. `relax-retry` 가 `p_color_family` 는 떨어뜨리지만 features 자체가 없으면 그 전에 탈락 |
| `search_products_v6` | `feature_metadata->>'gender'` | 3단 다리 ① |
| curation (`_quality_sql` + 하이드레이션), PDP (`/v1/products/{id}`) | `feature_metadata->>'gender'` | 3단 다리 ① |

**gender 3단 다리** — 크롤러가 gender 생성을 멈춘 시점과 VLM 이 채우는 시점 사이의
창을 메우는 임시 계단이다. 이 계약이 이행되면 ②를 걷어내고 `products.gender` 를 DROP 한다.

```
① feature_metadata->>'gender' 있으면  → 그 값으로 판정
② 없으면 products.gender (크롤러 레거시 배열)
③ 둘 다 없으면 → 검색: fail-open(양쪽 노출) / 큐레이션: fail-closed(제외)
```

③의 비대칭은 의도적이다. 메인 피드에 성별이 어긋난 상품이 노출되는 비용이 신규 상품
등장이 한 배치 늦는 비용보다 크고, 큐레이션 후보 풀(60)이 슬롯(12)보다 넉넉하다.

## 2. 현재 상태 (실측 2026-07-30)

```
product_features           131,058행   전량 feature_version='fashion_v1.1'
                                       vlm_model='Qwen3-VL-30B-A3B-Instruct-AWQ-4bit'
                                       전량 2026-07-28 20:41 단일 벌크 — 증분 경로 없음
in_stock 상품               88,411
  ├ features 보유            79,283
  └ features 미보유           9,128   ← 색상 검색에서 통째로 탈락 중
feature_metadata.gender 보유      1   ← 값이 'woman' (아래 §3-2 참조)
```

`feature_metadata` 100% 보유 키: `primary_color`, `secondary_colors`, `material`,
`pattern`, `style_tags`, `neckline`, `details`, `fit`.

`primary_color` 는 이미 **정확히 16 canonical family** (BLACK / WHITE / GREY / BLUE /
BROWN / CREAM / NAVY / GREEN / BEIGE / KHAKI / PINK / RED / YELLOW / PURPLE / MULTI /
ORANGE, 대문자) 로 `search_products_v6.p_color_family` enum 과 완전히 일치한다 —
**색상 쪽 계약은 이미 지켜지고 있다. 변경 요청 없음.**

features 미보유 9,128건은 대부분 7/27 온보딩 코호트다:
`8division` 29.7% / `sculpstore` 30.6% / `cayl` 51.8% / `yearsago` 70.0%.

## 3. 합의가 필요한 것

### 3-1. 대기열 소비 — `product_features_pending`

증분 경로가 없어 신규·누락 상품이 영구히 features 를 못 받는다. 대기열 뷰를 만들었다
(kiko.ai-app migration **097**).

```sql
SELECT product_id, image_url, images, brand, name, category, subcategory, platform, crawled_at
FROM public.product_features_pending      -- in_stock & features 없음 & image_url 있음
ORDER BY crawled_at DESC                  -- 권장: 최근 온보딩이 구멍의 대부분
LIMIT :n
```

쓰기는 upsert 로:

```sql
INSERT INTO public.product_features (product_id, retrieval_text, feature_metadata, ...)
VALUES (...)
ON CONFLICT (product_id) DO UPDATE SET ...
```

**클레임/락 테이블을 두지 않았다.** upsert 재실행이 안전하므로 단일 프로세스 전제로
가장 단순하게 뒀다. 배치를 여러 프로세스로 쪼갤 계획이 있으면 알려달라 — `claim` 컬럼과
`FOR UPDATE SKIP LOCKED` RPC 를 추가한다 (`claim_product_refresh_candidates` 와 같은 패턴).

진행률은 `public.product_features_coverage` 로 본다 (플랫폼별 `pct_featured` + `with_gender`).

> ⚠️ 097 은 아직 **DB 에 미적용**이다. 적용 전에는 위 두 뷰가 존재하지 않는다.

### 3-2. `gender` 값 어휘 — 소문자 스칼라

```jsonc
"gender": "men" | "women" | "unisex"
```

**배열이 아니라 스칼라**를 요청한다. VLM 은 이미지 1장으로 판정하므로 다중값이 나올 수
없고, RPC 술어가 `p.gender && ARRAY[...]` → `= :g` 로 단순해진다.

⚠️ **현재 유일한 샘플 1건의 값이 `'woman'`(단수형) 이다.** 그대로 확장되면 소비 측
전부와 어긋난다 — `'woman' = 'women'` 은 false 라 그 상품은 어느 성별 검색에도 안 잡힌다.
migration **095** 가 이미 어휘를 제약으로 고정해 뒀다:

```sql
ALTER TABLE product_features ADD CONSTRAINT chk_pf_gender_vocab
  CHECK (feature_metadata->>'gender' IS NULL
         OR feature_metadata->>'gender' IN ('men','women','unisex')) NOT VALID;
```

`NOT VALID` 라 기존 행은 통과하지만 신규 INSERT/UPDATE 는 검사된다. 즉 `'woman'` 으로
쓰면 **DB 가 거부한다.**

### 3-3. 커버리지 목표

`products.gender` 컬럼 DROP(그리고 3단 다리 ② 제거)은 **검색 실모수 기준 gender
커버리지 95% 이상**에서 착수한다. 검색 실모수 = `in_stock` ∩ `product_embeddings`
= 82,397. 현재 gender 커버리지 1건이라 Track B 는 여기서 막혀 있다.

## 4. 미충족 시 무슨 일이 나는가

| 미충족 항목 | 결과 |
|---|---|
| pending 소비 안 함 | 신규·누락 상품이 **색상 필터에서 영구 탈락**. 현재 9,128건 (in_stock 의 10.3%) |
| `gender` 어휘 불일치 (`woman` 등) | migration 095 CHECK 가 INSERT 를 거부 → 그 배치 실패 |
| gender 미채움 | `products.gender` DROP 불가 → 레거시 컬럼과 3단 다리를 계속 유지. 크롤러가 gender 를 만들지 않으므로 **신규 상품은 큐레이션에서 제외**된다(fail-closed) |

## 5. 검증

```sql
-- 커버리지 진행률 (플랫폼별)
SELECT * FROM public.product_features_coverage ORDER BY total DESC LIMIT 20;

-- gender 어휘 위반 감지 (0행이어야 정상)
SELECT feature_metadata->>'gender' AS g, count(*)
FROM public.product_features
WHERE feature_metadata ? 'gender'
  AND feature_metadata->>'gender' NOT IN ('men','women','unisex')
GROUP BY 1;

-- 검색 실모수 기준 gender 커버리지 (Track B 착수 판단)
SELECT count(*) AS universe,
       count(*) FILTER (WHERE f.feature_metadata ? 'gender') AS with_gender
FROM public.products p
JOIN public.product_embeddings e ON e.product_id = p.id
LEFT JOIN public.product_features f ON f.product_id = p.id
WHERE p.in_stock;
```

## 6. 열린 항목 (계약 밖, 후속 논의)

1. **`secondary_colors` 활용** — 현재 검색은 `primary_color` 단독 매칭으로 기존 의미론을
   보존했다. 리콜 개선용 후속 과제.
2. **`product_features.text_embedding`(halfvec 768 + HNSW) vs `product_embeddings`** —
   검색은 여전히 후자를 쓴다. 두 임베딩의 관계·통합 여부 확인 필요.
3. **`image_url` 변경 시 무효화** — 모델컷 선별(crawler, 휴면)이 `image_url` 을 바꾸면
   그 상품의 `product_features` 와 `product_embeddings` 가 둘 다 무효가 된다. 그 기능을
   살릴 때 재생성 큐가 필요하다.
