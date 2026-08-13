# 신규 브랜드 크롤링 — "단순 크롤링 이외의 효과적 방법" 실현가능성 검증

## Context

목표: 신규 브랜드를 온보딩할 때, 현재의 **단순 카테고리 크롤링** 대비 sitemap discovery / JSON-LD 같은 방법이 실제로 더 효과적인지 실물로 검증하고 실현 가능성을 정의한다.

작업 대상 코드베이스는 이 repo(ai-server, 검색 read-side)가 아니라 **`/Users/jaekwan/MyDrive/Project/kiko/crawler`** (Node/TS + Playwright, `products` 테이블에 write). ai-server는 같은 Postgres를 **읽기 전용**으로만 쓴다.

현재 신규 브랜드 온보딩의 실제 비용:
- **Cafe24 샵**(카탈로그 다수): `src/configs/platforms.ts` 에 `SiteConfig` 추가 + **샵마다 cateNo 7~20개를 손으로 매핑**(`category.discovery: "manual"`). 이게 가장 큰 수작업 비용.
- **Shopify**: `SiteConfig` 한 줄. `/products.json` 자동 사용 (이미 해결됨).
- **그 외**(Uniqlo/Zara/29cm/Farfetch): 사이트별 전용 엔진 작성 (고비용).

즉 "효과적 방법"이 실제로 겨냥해야 할 대상은 **Cafe24 신규 샵의 수동 cateNo 매핑 제거**와 **엔진 없는 일반 브랜드몰**이다.

## 검증 방법 (live probe, 2026-07-04)

Cafe24 샵 5곳 + Shopify 1곳에 대해 `robots.txt` / `/sitemap.xml` / 상품 상세 페이지를 실제 fetch 하여 (1) sitemap 상품 URL 노출 여부, (2) lastmod 유무, (3) 상품 페이지 JSON-LD/OG 구조화 데이터 유무를 측정.

## 검증 결과 (근거)

### sitemap 상품 URL 커버리지

| 샵 (Cafe24) | /sitemap.xml 상품 URL | lastmod | 비고 |
|---|---|---|---|
| shopamomento | ✅ 500+ (`/product/{slug}/{id}/`) | ✅ 전건 | 이상적 |
| havatishop | ✅ 1,052 (products 포함) | ✅ 대부분 | |
| adekuver | ✅ 800+ (`/product/...`) | ❌ (changefreq/priority만) | lastmod 없음 |
| triplestore | ✅ sitemap **index** → `sitemap0/1.xml.gz` (gzip) | ✅ (index) | 규모 커서 분할 |
| mardimercredi | ❌ 카테고리/board만, **상품 URL 0** | 불규칙 | 단일브랜드 샵 |
| kith (Shopify, 대조군) | ✅ robots.txt에 Sitemap 선언 | — | `/products.json` 이미 커버 |

→ **Cafe24 5곳 중 4곳(~80%)** 이 `/sitemap.xml` 기본 경로에서 상품 URL을 노출. URL에 **product_no(숫자 id)가 그대로 박혀 있음**. robots.txt가 Sitemap을 선언하지 않아도 기본 경로에 존재. `/product/` 는 어디서도 Disallow 아님.

### 상품 상세 페이지 구조화 데이터 (JSON-LD / OG)

| 상품 페이지 | JSON-LD Product | OpenGraph |
|---|---|---|
| shopamomento 상품 | ❌ 없음 | ❌ 없음 |
| havatishop 상품 | ❌ 없음 | ❌ 없음 |

→ **Cafe24 상품 페이지는 구조화 데이터를 내보내지 않음 (순수 HTML).** JSON-LD 파서를 넣어도 지배적 플랫폼인 Cafe24에서는 사실상 100% DOM fallback으로 떨어진다.

## 실현 가능성 정의 (방법별)

1. **Cafe24 sitemap discovery — 효과적, 저비용, 채택 권장 (HIGH)**
   - ~80% Cafe24 샵에서 상품 URL + product_no를 sitemap 한 번으로 확보 → **샵당 cateNo 수동 매핑 제거**. 이게 신규 온보딩 비용을 가장 크게 줄이는 지점.
   - 단, **범용 대체 아님**: mardimercredi처럼 sitemap에 상품이 없는 샵이 존재 → 기존 category-crawl을 **fallback**으로 유지해야 함.
   - lastmod 증분 재크롤은 **기회적**(있는 샵만) — 보장된 메커니즘으로 설계하면 안 됨.

2. **JSON-LD 상세 파서 — 이 카탈로그에는 비효과적 (LOW)**
   - 지배적 플랫폼 Cafe24가 JSON-LD/OG를 전혀 내보내지 않음 → 원 계획의 "JSON-LD 1순위"는 이 카탈로그 기준 근거 없음. DOM 셀렉터 파서(`registry-detail-parser` + 18개 golden)가 계속 필수.
   - JSON-LD가 값을 갖는 곳은 **엔진 없는 해외/일반 브랜드몰** 소수뿐. 따라서 "일반 사이트 경로" 한정 **부가 enrichment**로만 스코프.

3. **Shopify — 이미 해결됨**: `/products.json` 이 JSON-LD보다 풍부. 추가 작업 불필요.

4. **이미지 게이트 — 부분적으로 이미 존재, 별도 트랙**: `select-representatives.ts:110 isUsableImage()` 가 icon/logo/badge + 차단 CDN 제외를 이미 수행(analyze-products.ts 공유). 이 시스템의 임베딩 대상은 `images[0]`가 아니라 단일 `image_url` 컬럼. 저해상도/중복 판정만 신규. 이 트랙은 discovery와 독립.

## 권고 방향

원 계획을 다음과 같이 재조정한다:

- **핵심 채택**: 신규 브랜드 온보딩용 **플랫폼 인지 discovery**. Shopify=`/products.json`(기존), **Cafe24=`/sitemap.xml` 상품 URL discovery(신규)** + category-crawl fallback, 일반몰=sitemap + JSON-LD.
- **격하**: JSON-LD를 detail 1순위에서 빼고 "엔진 없는 일반몰" 경로 한정 부가 소스로.
- **분리**: 이미지 게이트는 별도 저위험 트랙 (원하면 병행).

## 다음 단계

1. **Phase 0 — 커버리지 프로브 확장 (go/no-go, 코드 커밋 없음)**
   - 현재 활성 Cafe24 샵 전체(`getActivePlatforms()` 중 `type==="cafe24"`)에 대해 `/sitemap.xml` 상품 URL 유무 + lastmod 유무 + sitemap 상품 수 vs 현재 category 크롤 상품 수를 일괄 측정.
   - 판정: sitemap 상품 커버리지가 (예) ≥70% 샵이면 discovery 레이어 진행. 미만이면 category-crawl 유지 + sitemap을 보조로만.
   - triplestore류 `.gz` / sitemap index 파싱 확인 포함.

2. **Phase 1 — Cafe24 sitemap discovery 레이어 (Phase 0 통과 시)**
   - `SiteConfig` 에 opt-in `category.discovery: "sitemap"` 추가. discovery 단계 산출을 `{ productUrl, productNo, lastmod? }[]` 로 통일 → 기존 상세 파서/import 재사용.
   - sitemap 없음/상품 0 → 기존 category-crawl 자동 fallback. robots-check(`src/lib/robots-check.ts`) 그대로 통과.
   - 대표 수정 지점: `src/crawl.ts`(discovery 분기), `src/configs/platforms.ts`(타입), 신규 `src/lib/sitemap-discovery.ts`.

3. **(선택) JSON-LD** 는 일반몰 신규 온보딩이 실제로 생길 때 `parser-strategy.ts` 레지스트리에 부가 전략으로. 지금은 보류.

## Verification (이 검증 자체의 재현)

위 표의 결론은 `WebFetch` 라이브 프로브로 재현 가능:
- `https://{shop}/sitemap.xml` (상품 URL/lastmod 확인) — shopamomento/havatishop/adekuver/triplestore/mardimercredi
- `https://{shop}/product/.../{id}/` (JSON-LD/OG 부재 확인)
- Phase 0 스크립트는 crawler repo에서 `getActivePlatforms()` 순회 + 각 baseUrl `/sitemap.xml` HEAD/GET 로 자동화.
