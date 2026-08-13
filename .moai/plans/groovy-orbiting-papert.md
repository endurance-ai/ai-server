# 메인 큐레이션 + 온보딩 API 4종 — 스펙 검토 및 구현 계획

## Context (검토 결과)

앱(iOS) 신규 기능: 온보딩(성별 + 브랜드 픽) & 메인 큐레이션(server-driven 구좌 + 유도 칩).
"4개 API 추가하면 된다"는 **엔드포인트 수로는 맞지만**, 실제로는 지원 작업이 함께 필요하다:

- **마이그레이션 1개** (`ai.curation_sections` 캐시 테이블 + `ai.user_brand_picks` + `user_profiles.onboarded_at`)
- **인앱 백그라운드 갱신 태스크** (auto 구좌 계산 + 노션 editorial 동기화) — 사용자 결정: 인앱 asyncio 주기 태스크
- **칩 상수 모듈** — 사용자 결정: 서버 코드 상수 (교체 = 서버 배포, '앱 배포 불필요' 요건 충족)
- **optional-auth dependency** (기존 `get_current_user_id`는 required-only)

이 repo는 이미 앱의 `/v1` 전체를 서빙 중 (auth JWT, chat, me, products, saves, results...) — 4개 API 모두 여기 소속이 맞음.

### 스펙 갭 / 확인 필요 사항 (검토에서 발견)

| # | 이슈 | 처리 |
|---|------|------|
| 1 | **gender 값 불일치**: 스펙 `women\|men` vs `ai.user_profiles` CHECK `male\|female\|other` (migrations/versions/0009) | API 경계에서 매핑 (women↔female, men↔male). DB 마이그레이션 불필요 |
| 2 | **`brand_nodes.gender_scope` 컬럼이 코드에 없음** (스펙은 이 컬럼으로 구좌 브랜드 필터한다고 함). 코드는 `attributes->>'gender_lean'`만 사용 (brand_node_cache.py) | 구현 시 dev-app DB에서 컬럼 존재 확인 → 없으면 `gender_lean` + `products.gender` 로 필터 |
| 3 | **대표 브랜드 그리드 소스 부재** — `/v1/brand-nodes` 계약은 `{id,name}` 21개뿐 | 사용자 결정: 응답에 `representative_brands` 확장 (노드당 상품 수 상위 N 브랜드) |
| 4 | **남성 칩 등록 시점 충돌**: v1.1 "men 골든셋(윤영 7/15) 전까지 빈 배열" vs 칩 셀 확정 "남성 4종 (현규 실검색 S)" | 상수에 남성 4종을 넣되 배포 시점에 골든셋 결과 확인 후 활성화 (상수라 배포 게이트가 곧 검증 게이트) |
| 5 | Under $100 구좌 — v6 검색에 가격 필터 없음 (스펙에서 이미 지적) | `public.products.price < 100 AND in_stock` 직접 SQL (products 테이블에 price/gender/brand_node_id 있음 — app/api/products.py:130 확인) |
| 6 | 반응 로깅 ②(section_id/chip_id 프로퍼티)는 클라 앰플리튜드 작업, ①(추천 파이프라인 소비 경로)는 별도 결정 건 | **이 계획 범위 외** — 서버 변경 없음 |
| 7 | 기존 `GET /v1/style-nodes` 와 신규 `/v1/brand-nodes` 는 같은 21행(`style_nodes`)을 다른 형태로 노출 | 신규 엔드포인트 추가 (계약 준수), style-nodes는 그대로 |
| 8 | 브랜드 수: 스펙 3,776 vs brand_node_cache 문서 2,899 (시점 차이) | 정보성 — 영향 없음 |

### 재사용할 기존 자산

- `app/api/deps.py` `get_current_user_id` — optional 변형 추가
- `app/infrastructure/repositories/style_node.py` `list_nodes()` (21노드, lifespan warm 완료)
- `app/infrastructure/repositories/brand_node_cache.py` `normalize_brand()` — 브랜드 검색 정규화
- `app/api/results.py:149` `unnest(product_ids) WITH ORDINALITY JOIN products` — product_ids → 카드 하이드레이션 SQL 패턴
- `app/api/chat.py` `ProductRef` / `app/api/products.py` `ProductRef` — 카드 스키마 (curation `products[]`에 동일 형태 사용)
- 인기 신호: `ai.product_views`(0011), `ai.saves`(0010), `ai.searches`(0015 — title로 트렌딩 검색)
- lifespan warm 패턴: `brand_node_cache.warm_cache()` 방식 (fail-open)

---

## 구현 계획

### 1. Migration `migrations/versions/0020_add_curation_and_onboarding.py`

```sql
ai.curation_sections (
  section_id  TEXT,             -- 'popular' | 'trending-search' | 'under-100' | 'editorial-*'
  gender      TEXT,             -- 'women' | 'men'
  slot_type   TEXT NOT NULL,    -- 'auto' | 'editorial'
  title       TEXT NOT NULL,
  subtitle    TEXT,
  product_ids BIGINT[] NOT NULL DEFAULT '{}',
  sort_order  INT NOT NULL DEFAULT 0,
  is_active   BOOLEAN NOT NULL DEFAULT true,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (section_id, gender)
)
ai.user_brand_picks (
  user_id UUID REFERENCES ai.user_profiles ON DELETE CASCADE,
  brand_id BIGINT NOT NULL,     -- public.brand_nodes.id (원신호 보존, 노드 유도는 조회 시)
  source TEXT NOT NULL DEFAULT 'onboarding',
  created_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (user_id, brand_id)
)
ALTER TABLE ai.user_profiles ADD COLUMN onboarded_at TIMESTAMPTZ  -- 재로그인 재확인 판단용
```

### 2. `app/api/deps.py` — `get_optional_user_id`

`HTTPBearer(auto_error=False)` + 토큰 없거나 무효면 `None` 반환 (곡선 없이 기존 함수 옆에 추가).

### 3. `app/api/brand_nodes.py` — `GET /v1/brand-nodes` (no auth)

- `style_node.list_nodes()` 21개 → `{ nodes: [{ id, name, representative_brands: [{id, name}] }] }`
- 대표 브랜드: `public.brand_nodes` ↔ `public.products.brand_node_id` 상품 수 기준 노드당 상위 6개, lifespan warm 시 1회 계산해 메모리 캐시 (brand_node_cache 패턴, fail-open — 실패 시 빈 배열)

### 4. `app/api/brands.py` — `GET /v1/brands/search?q=` (no auth)

- `brand_name ILIKE %q%` OR `brand_name_normalized LIKE normalize_brand(q)%`, LIMIT 8
- 응답 `{ brands: [{ id, name, node_id }] }` (`node_id` = `primary_style_node_id`)

### 5. `app/api/onboarding.py` — `POST /v1/onboarding` (auth required)

- body `{ gender: 'women'|'men', selected_brand_ids: number[] }` (Pydantic Literal 검증, brand_ids 상한 예: 50)
- 트랜잭션: `user_profiles.gender`(매핑값) + `onboarded_at=now()` UPDATE → `user_brand_picks` upsert (재로그인 재확인 = 기존 picks 삭제 후 재삽입)

### 6. `app/api/curation.py` — `GET /v1/curation?gender=` (optional auth)

- gender 해석: 로그인+프로필 gender 있으면 **프로필 우선**, 아니면 query param, 둘 다 없으면 400
- `ai.curation_sections` (is_active, gender, sort_order) 조회 → product_ids를 results.py:149 패턴으로 하이드레이션 → `sections[]`
- `chips[]`: 상수 모듈에서 gender별 반환
- 섹션 0개여도 200 + 빈 배열 (클라 캐시 폴백 전제)

### 7. `app/services/curation_chips.py` — 칩 상수

- 여성 5종(스펙 JSON 그대로: mood/aesthetic/fit 문형), 남성 4종(크롭 반팔티 / 카모 카고 / 루즈핏 데님 / 인디 밴드 티) — `label_ko` ≠ `query_en` 분리, `category` 포함
- 배포 전 골든셋 확인 주석 명시 (금지: 가격 조건·부정형·블랙리스트 값)

### 8. `app/services/curation_refresh.py` — 인앱 백그라운드 갱신

- lifespan에서 `asyncio.create_task` 루프, `CURATION_REFRESH_INTERVAL_S` (기본 3600), 전체 fail-open (실패 시 기존 행 유지 = stale-while-error)
- **popular**: 최근 7d `ai.product_views` + `ai.saves` 브랜드별 집계 상위 → 브랜드 대표 상품
- **trending-search**: 최근 `ai.searches` 빈도 상위 결과셋의 product_ids 상위
- **under-100**: `products WHERE price < 100 AND in_stock AND gender 매칭` 인기순
- **editorial**: 노션 "큐레이션 구좌" DB를 Notion REST(httpx 직접, `NOTION_TOKEN` + `NOTION_CURATION_DB_ID`)로 읽어 upsert — 활성 체크박스 off → `is_active=false`
- gender 필터: `brand_nodes.gender_scope` 존재 확인 후 사용, 없으면 `products.gender` + `gender_lean` 폴백 (갭 #2)

### 9. 배선

- `app/api/__init__.py` 라우터 3개 등록 (brand_nodes, brands, onboarding, curation — 4파일)
- `app/main.py` lifespan에 refresh 태스크 시작/취소
- `app/core/config.py`: `NOTION_TOKEN`, `NOTION_CURATION_DB_ID`, `CURATION_REFRESH_INTERVAL_S`
- `.env.example` 갱신

### 10. 테스트

`tests/test_auth/test_products_api.py` 패턴 따라 라우터별 테스트: gender 매핑 왕복, optional-auth 분기(비로그인+param / 로그인 프로필 우선), onboarding upsert 멱등성, 칩 gender 분기, 섹션 하이드레이션 순서 보존, refresh fail-open.

## 검증

1. `uv run ruff check . && uv run ruff format --check .` → `uv run pytest`
2. 로컬 `uv run uvicorn app.main:app --port 8000` (DB_DSN 로컬/dev) 후:
   - `curl /v1/brand-nodes` → 21노드 + 대표 브랜드
   - `curl "/v1/brands/search?q=alyx"` → limit 8
   - `curl -X POST /v1/onboarding` (JWT) → 200, DB에 gender='female'/picks 반영 확인
   - `curl "/v1/curation?gender=women"` → sections(빈 배열 허용) + chips 5종; men → chips 상태는 배포 게이트 결정대로
3. refresh 1회 수동 트리거 후 `ai.curation_sections` 행 확인, 노션 활성 토글 → 다음 주기 반영 확인

## 범위 외 / 후속

- 추천 파이프라인용 반응 로그 소비 경로 (①) — 별도 결정 건
- section_id/chip_id 앰플리튜드 프로퍼티 — 클라(윤영) 작업
- 칩 개인화·DB/노션 이전 — v2
- 완료 후 노션 "API 명세서 (화면 단위 엔드포인트)" DB에 4개 엔드포인트 문서 추가
