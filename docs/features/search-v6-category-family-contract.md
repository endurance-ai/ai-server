# Search v6 — Category Family Guard (B) 계약 prep

상태: **WAITING ON SHARED CONTRACT** (app-side가 실 752 → canonical family 확정 후 진행)
범위: kikoai/ai 봇 client-side soft family 가드(B). DB 마이그는 kiko.ai 소유.

---

## 1. 왜 하드필터 불가 (확정 근거)

라이브 `products.category` 전수 조사 결과 — 비정규화 다국어 long-tail:

- 스페인(Inditex 내부코드): `ABRIGO / B. Abrigo/Gab`, `CAZADORA / T.P-EXTER.LARGA`, `BISUTERIA / BIS. COLLAR`
- 이탈리아: `STIVALI`, `BORSE A TRACOLLA`, `SCARPE STRINGATE`
- 대소문자/오타 혼재: `Top`/`Tops`/`shoes`/`cardigan`/`T-Shrits`
- 비패션 노이즈: `Insurance`, `Donation`, `Gift Cards`, `Package Protection`, `Skateboard`, `candle`, `Bedding`, `OBJECTS`

`search_products_v6.p_category` = `products.category` 단일문자열 **exact-match** → Vision 어휘로는 매칭 불가, hard filter는 0건 구멍. **카테고리 가드는 RPC 이후 client-side soft만 가능.**

## 2. 하드 전제 (app-side 072 스코프 必)

> **v6 RPC가 결과행에 `category_canonical`(family)을 RETURN 해야 한다.**

근거: 봇은 752값→family 다국어 클러스터링을 재현 불가(=2층 중복·드리프트 = 막으려는 그 실패). v6가 family를 안 돌려주면 **봇 client-side B는 구현 자체가 불가능**. → 072 v6 개편 `RETURNS TABLE` 에 `category_canonical text` 추가 필수.

현 v6 RETURNS: `id, brand, name, price, image_url, product_url, platform, subcategory, distance, degraded` → **`category_canonical` 누락, 추가 요청.**

## 3. 공유 단일 계약 = canonical family 택소노미

- app-side가 실 752 `products.category` 에서 family 셋 도출 → **이게 1·2 공유 계약**.
- 봇 Vision 입력 어휘는 **이미 깨끗한 고정 7종** (변경 불가, app `analyze.ts` 동치 — `app/channels/vision_prompt.py:149`):
  - category: `Outer, Top, Bottom, Shoes, Bag, Dress, Accessories`
  - subcategory(발췌): Outer→overcoat/trench-coat/blazer/cardigan/... · Top→t-shirt/shirt/sweater/... · Bottom→jeans/trousers/shorts/skirt/... · Shoes→sneakers/boots/loafers/... · Bag→tote/crossbody/backpack/... · Dress→mini/midi/maxi/... · Accessories→hat/scarf/belt/...
- **family 셋이 이 7종 입도와 정렬되면 Vision→family ≈ 항등.** app-side family 설계 시 이 7종을 입력 신호로 권고(강제는 app 판단).

## 4. 봇-side B 통합 지점 (계약-무관, 사전 확정)

1. **Vision category plumbing (선결, 현재 누락):** `search_products.py:307` 이 `style_node_primary`(스타일노드 A~Z)를 `category`로 오라벨링. 실제 Vision garment category는 `state.vision_selected_item.category`/`state.detected_items[selected_item_index]` 에만 존재 → tool ctx/args로 미전달. B는 이 값을 검색 경로로 plumb 해야 함(스타일노드 오라벨은 별개로 둠).
2. **삽입 seam:** post-RPC. `run_text_only_search` + `run_image_search` 양쪽이 공유하는 단일 지점(`search_service` 직후 ~ `diversify` 전, 또는 persist 전) — 한 곳으로 두 경로 커버.
3. **메커니즘 (계약-shaped, 값은 미정):**
   - `family_of_row(row) = row["category_canonical"]` (v6 RETURN, 전제 2)
   - `family_of_vision(vision_cat/subcat) → family` (고정 7-enum → 공유 family 매핑, 1줄 데이터)
   - 불일치 행 = **후순위 페널티(드롭 아님)**, 필터 후 비면 임베딩 top 유지(**0건 floor**)
   - 미매핑 family → OTHER → 통과(가드 비활성, 안전측)

## 5. app-side 위임에 넣을 항목 (체크리스트)

- [ ] 072 v6 `RETURNS TABLE` 에 `category_canonical text` 추가 (전제 2 — 없으면 봇 B 불가)
- [ ] `category_canonical` 테이블/뷰: 752 raw → family (다국어·비패션 OTHER 처리 포함)
- [ ] 072 degraded fallback 과 `category_canonical` 정합 (degraded 시에도 family 채워짐 보장)
- [ ] v6 시그니처 안정 (param/return 추가는 append-only, 기존 컬럼 순서 불변)
- [ ] **family 리스트 회수** → 이게 봇 §4.3 `family_of_vision` 매핑 + Vision 프롬프트 enum 의 단일 소스

## 6. family 리스트 수령 후 봇 작업 (예정, 추측 금지)

1. Vision category plumbing (§4.1)
2. `category_buckets.py` 단일 소스: 고정7 → family 매핑 + soft 가드 순수함수(페널티+floor)
3. 단일 seam 배선(§4.2) — 두 경로 커버
4. 테스트: 합성 family로 메커니즘 + 실 family로 매핑 + 0건 floor
5. 미해결 회수·보고

---

수령 대기: ① 확정 canonical family 리스트 ② 갱신 Vision/AI 프롬프트 ③ 072 v6 `category_canonical` RETURN 여부 확인
