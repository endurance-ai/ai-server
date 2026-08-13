# Matteveil 제품 크롤링 → 임베딩 파이프라인 브리핑

> 이 문서는 다른 PC / 새 세션에서 작업을 이어받을 때 Claude에게 먹이는 프롬프트 초안입니다.
> 아래 내용을 그대로 새 대화 첫 메시지로 붙여넣으면 됩니다.

---

## 프롬프트 (복사해서 붙여넣기)

---

kiko 패션 추천 서비스에서 **Matteveil** 브랜드 제품을 크롤링 → DB 적재 → 임베딩까지 진행해줘.

### 프로젝트 구조

- 크롤러: `/Users/jaekwan/MyDrive/Project/kiko/crawler/` (TypeScript + Playwright)
- AI 서버: `/Users/jaekwan/MyDrive/Project/kiko/ai-server/` (Python + FastAPI)
- DB: dev-app Postgres (`public.products` + `public.product_embeddings`)

### Step 0 — 플랫폼 타입 확인

Matteveil 사이트(`matteveil.com`)가 **Cafe24인지 Shopify인지** 먼저 확인해야 해.

방법:
1. 브라우저로 `matteveil.com` 카테고리 페이지 URL 확인 → `?cate_no=숫자` 패턴이면 Cafe24
2. 또는 페이지 소스에서 `cafe24` / `Shopify` 문자열 탐색
3. 또는 `npx tsx src/crawl.ts --probe=matteveil` (crawler 디렉토리에서)

### Step 1 — platforms.ts에 설정 추가

파일: `crawler/src/configs/platforms.ts`

**Cafe24일 경우** (한국 자사몰 대부분):
```typescript
{
  key: "matteveil",
  name: "Matteveil",
  type: "cafe24",
  baseUrl: "https://matteveil.com",
  paginate: true,
  maxPages: 300,
  category: {
    discovery: "manual",
    categories: [
      // 사이트 카테고리 URL에서 ?cate_no=숫자 확인 후 채우기
      { name: "Outer",       cateNo: ???, gender: ["women"] },
      { name: "Top",         cateNo: ???, gender: ["women"] },
      { name: "Bottom",      cateNo: ???, gender: ["women"] },
      { name: "Dress",       cateNo: ???, gender: ["women"] },
      { name: "Accessories", cateNo: ???, gender: ["women"] },
    ],
  },
  notes: "Women 자사몰",
},
```

**Shopify일 경우**:
```typescript
{
  key: "matteveil",
  name: "Matteveil",
  type: "shopify",
  baseUrl: "https://matteveil.com",
  maxPages: 300,
  crawlDelay: 1500,
},
```

기존 패턴 참고: `platforms.ts` 안에 `shopamomento`, `aime-leon-dore` 등 예시 많음.

### Step 2 — 크롤링

```bash
cd /Users/jaekwan/MyDrive/Project/kiko/crawler

# 설정 검증 (상품 안 긁음)
npx tsx src/crawl.ts --dry-run --site=matteveil

# 실 크롤
npx tsx src/crawl.ts --site=matteveil
```

출력: `data/matteveil-products.json`

검증:
```bash
cat data/matteveil-products.json | npx -y jq 'length'
cat data/matteveil-products.json | npx -y jq '.[0:3] | .[] | {name, price, gender, imageUrl}'
```

### Step 3 — DB 임포트

```bash
cd /Users/jaekwan/MyDrive/Project/kiko/crawler

# .env.local에 DB_URL, DB_TOKEN 필요
npx dotenv -e .env.local -- npx tsx src/import-products.ts --site=matteveil
```

- `product_url` unique key UPSERT → 중복 실행 안전
- brand_nodes에 "Matteveil" 자동 INSERT
- `SELF_BRANDED` 맵(`import-products.ts` 상단)에 `matteveil: "Matteveil"` 추가 필요할 수 있음 (brand 필드가 비어오는 자사몰용)

### Step 4 — 임베딩

```bash
cd /Users/jaekwan/MyDrive/Project/kiko/ai-server

# 처음 1회
uv sync --group embed

export KIKOAI_DEVAPP_DSN='postgresql://app_user:PASS@54.116.104.193:5432/kikoai?sslmode=require'

# 검증 (50개, DB write 없음)
uv run python scripts/embed_batch_devapp.py --limit 50 --dry-run

# 실행 (미임베딩 전체 처리 — Matteveil 포함)
uv run python scripts/embed_batch_devapp.py
```

- Apple Silicon MPS 자동 사용 (수백 개면 몇 분 이내)
- 중단 후 재실행 안전 (anti-join으로 이미 된 것 건너뜀)
- 완료 후 `product_embedding_coverage` 뷰에서 matteveil 행 100% 확인

### 핵심 파일 위치

| 역할 | 경로 |
|------|------|
| 플랫폼 설정 추가 | `crawler/src/configs/platforms.ts` |
| 크롤 결과 JSON | `crawler/data/matteveil-products.json` |
| DB 임포트 스크립트 | `crawler/src/import-products.ts` |
| 임베딩 배치 | `ai-server/scripts/embed_batch_devapp.py` |

### 주의사항

- 완전 커스텀 사이트(cafe24/shopify 아님)라면 `crawler/src/lib/matteveil-engine.ts` 신규 작성 필요
- Cafe24 `cateNo`는 반드시 실제 사이트 URL에서 직접 확인해야 함 (추측 금지)
- DB_URL/DB_TOKEN은 `crawler/.env.local`, KIKOAI_DEVAPP_DSN은 별도 export

---

*작성일: 2026-06-28*
