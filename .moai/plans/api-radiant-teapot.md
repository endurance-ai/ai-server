# 큐레이션 구좌 20개 컷 제거 — 큐레이팅한 상품 전부 노출

## Context

`GET /v1/curation` 은 구좌별로 저장된 `product_ids` 를 앞 20개만 잘라서 내려준다
(`_PRODUCTS_PER_SECTION = 20`). 그런데 운영자가 실제로 큐레이팅해 넣은 목록은
그보다 훨씬 길다 — `scripts/seed_curation_sections.py` 기준:

| 구좌 | 저장된 개수 | 지금 노출 |
|------|------------|----------|
| `editorial-summer-vacation` / women | 84 | 20 |
| `editorial-summer-vacation` / men | 43 | 20 |
| `editorial-bermuda-pants` | 30 | 20 |
| `editorial-swimwear` / women | 34 | 20 |

즉 고른 상품의 3/4 가 조용히 버려지고 있다. 이 컷을 없애서 **editorial(수동 큐레이션)
구좌는 넣은 만큼 전부** 노출한다.

범위 결정 (사용자 확인 완료):
- **auto 구좌(popular / trending-search / under-100)는 12개 그대로 유지.** 리프레셔가
  후보 60개 중 브랜드당 2개 캡으로 정확히 12개를 뽑고, 12개가 안 되면 아예 비우는
  로직(`_SECTION_SIZE`, `require_full`)은 건드리지 않는다.
- **저장 상한 200 유지.** `SectionPayload.product_ids` 의 `max_length=200` 이 곧
  구좌당 실질 천장이 된다 (현재 최대 84개라 여유 있음).

따라서 이 작업은 "새 상한을 정하는" 게 아니라 **API 읽기 경로의 truncation 을
제거**하는 것이다. 상한은 이미 쓰기 단(200)과 리프레셔 쿼터(12)에 각각 존재하므로,
읽기 단에서 한 번 더 자를 이유가 없다.

## 변경 대상

### 1. `app/api/curation.py` — 실제 서빙 컷 제거 (핵심)

`_PRODUCTS_PER_SECTION = 20` (`:35`) 상수를 **삭제**하고, 이를 쓰는 세 곳의 슬라이스를
없앤다:

- `:138` editorial(및 비개인화) 경로 — `(product_ids or [])[:_PRODUCTS_PER_SECTION]`
  → `(product_ids or [])`
- `:174` auto 개인화 실패 시 폴백 — 같은 방식. auto 구좌는 DB에 정확히 12개만
  들어 있으므로 슬라이스는 원래 무동작이었다.
- `:216` 2차 루프의 `selected_by_section.get(..., default)` — default 값도 동일하게
  슬라이스 제거. (`:176` 에서 모든 section_id 가 채워지므로 실질 dead default 지만
  규칙을 일치시킨다.)

auto 구좌를 12개로 묶는 건 `curation_refresh.py` 의 쿼터이지 이 상수가 아니므로,
상수를 지워도 auto 동작은 그대로다.

### 2. `app/api/admin_curation.py:281` — 어드민 프리뷰의 중복 리터럴 제거

```python
candidates = [int(p) for p in (product_ids or [])][:20]
```
→ `[:20]` 제거. 이 엔드포인트의 계약은 "앱에 실제로 뜨는 수를 재현한다"(`:261-266`
docstring) 이므로, API 쪽 컷만 풀고 여기를 남기면 20개 초과 구좌에서 `shown` 이
계속 과소 보고된다. 반드시 같이 간다.

### 3. `scripts/seed_curation_sections.py` — 복제 상수 제거

- `:51` `_PRODUCTS_PER_SECTION = 20` (curation.py 의 복사본) 및 `:49-50` 주석 삭제
- `:439` `claimed[gender].update(deduped[:_PRODUCTS_PER_SECTION])`
  → `claimed[gender].update(deduped)`
  `claimed` 는 "editorial 이 이미 쓴 상품을 브랜드 구좌에서 빼기" 용도라, editorial 이
  전량 노출되는 이상 전량을 claim 해야 브랜드 구좌와 겹치지 않는다.
- `:44-45` 주석 "브랜드 9개 × 2 = 18 개까지 후보가 쌓이고, **API 가 앞 20 개를 쓴다**"
  → 마지막 절이 사실이 아니게 되므로 수정 (`_BRAND_SECTION_POOL = 24` 가 전부 노출됨).

### 4. `app/api/curation.py:79` — 임프레션 배치 상한 (방어적 동반 수정)

`items: list[CurationImpression] = Field(min_length=1, max_length=50)`.
84개짜리 구좌가 뜬 뒤 클라가 한 번에 임프레션을 올리면 422 로 떨어지고, 그러면
개인화 학습이 조용히 끊긴다. `max_length=200` 으로 올려 저장 상한과 맞춘다.
(`position` 의 `le=100` 은 최대 구좌가 84개라 지금은 여유가 있으나, 200 상한과
맞추려면 `le=200` 이 일관적이다 — 함께 올린다.)

## 영향 없는 것 (확인 완료)

- **auto 구좌**: `curation_refresh.py` 의 `_SECTION_SIZE=12` / `_CANDIDATES_PER_SECTION=60`
  / 브랜드 랭크 캡 / freshness 의 `cardinality = 12` 전부 그대로. 손대지 않는다.
- **어드민 UI**: 이 저장소에는 없다 (#192 에서 제거됨). 남은 건 JSON API 뿐이고,
  UI 는 `kiko.ai-app` 의 `src/app/admin/curation/page.tsx` 에 있다. 거기서는 상한을
  강제하지 않으므로 앱 쪽 변경 불필요 — 다만 프리뷰 숫자가 커진다.
- **응답 스키마**: `CurationSection.products` 는 페이지네이션 필드가 없는 순수 리스트라
  스키마 변경이 없다. iOS 계약 그대로.
- **기존 테스트**: 20 컷을 assert 하는 테스트는 하나도 없다. 최대 editorial 픽스처가
  2개(`test_admin_curation_api.py:98-105`), `[12,12,12]` assert
  (`test_curation_onboarding_api.py:445`)는 auto 구좌의 `_SECTION_SIZE` 검증이다.

## 부수 효과 (의도된 것)

- 응답 크기 증가: women 첫 화면 기준 대략 20+20+20 → 84+30+34 수준. `products` 항목이
  8필드 스칼라라 페이로드는 여전히 수백 KB 미만이다. `Cache-Control: private, no-store`
  는 유지.
- 하이드레이션 쿼리(`p.id = ANY(%(ids)s)`, `curation.py:187-201`)의 IN-list 가 커지지만
  여전히 단일 쿼리이고 `p.id` 는 PK 다.
- 앞 구좌가 더 많은 상품을 선점하므로(`excluded_ids` 누적, `:177`) 뒤 구좌가
  더 얇아질 수 있다. 이건 원래 있던 동작이고, 어드민 `/preview` 로 확인 가능하다.

## 검증

1. `uv run ruff check . && uv run ruff format --check .`
2. `uv run pytest tests/test_auth/test_curation_onboarding_api.py tests/test_auth/test_admin_curation_api.py tests/test_curation_refresh.py`
   — 이 3개는 Testcontainers Postgres 를 쓰므로 도커가 떠 있어야 한다.
3. **회귀 테스트 추가** (`tests/test_auth/test_curation_onboarding_api.py`):
   기존 `_insert_section` 헬퍼(`:100-121`)를 재사용해 editorial 구좌에 25개 이상의
   in-stock 상품을 넣고 `GET /v1/curation` 이 25개를 전부 돌려주는지 assert.
   이게 이번 변경의 유일한 새 계약이다.
4. `uv run pytest` 전체 (커밋/PR 전 필수).
5. 수동: 로컬 서버 기동 후 `GET /v1/curation?gender=women` 의 각 섹션
   `len(products)` 와 `GET /admin/curation/preview?gender=women` 의 `shown` 이
   서로 일치하는지 대조 — 두 경로의 컷이 함께 풀렸는지 확인하는 가장 빠른 방법.
