# Kiko 골든셋 판정 결과 — 최종 (여성 2026-07-13 / 남성 2026-07-14)

메인 홈 개편의 칩(입력 유도) 설계 근거. 성별별 7개 문형 × 20개 값 = 140개 쿼리를
실제 운영 검색 경로(FashionSigLIP 임베딩 → `search_products_v6` → 다양성 캡,
색 하드필터 off)로 실행하고, 윤영이 top-10 결과를 눈으로 전수 판정.
남성은 남성 전용 매트릭스(드레스/스커트류 제거, 남성 헤리티지·스트릿 값) 사용.

- 하네스: `scripts/goldenset/run_goldenset.py` (+ `matrix.py`, `--gender men` 지원)
- 원시/판정: `out/results{,_men}.json`, `out/judgments{,_men}.json`, `out/report{,_men}.html`
- 판정: S(성공) / M(애매) / F(실패 — 칩 노출 시 신뢰 훼손)

## 문형별 성적 (성별 비교)

| 문형 | 여성 S/M/F (S율) | 남성 S/M/F (S율) |
| --- | --- | --- |
| [무드]+[카테고리] | 19/0/1 (**95%**) | 9/11/0 (45%) |
| [소재]+[카테고리] | 18/2/0 (90%) | 10/8/2 (50%) |
| [핏/실루엣]+[카테고리] | 17/1/2 (85%) | 12/8/0 (**60%**) |
| [컬러]+[카테고리] | 15/4/1 (75%) | 9/8/3 (45%) |
| [에스테틱/씬]+[아이템] | 8/7/5 (40%) | 12/5/3 (**60%**) |
| [패턴/디테일]+[카테고리] | 11/4/5 (55%) | 8/9/3 (40%) |
| [상황/TPO]+[아이템] (대조군) | 8/9/3 (40%) | 12/6/2 (**60%**) |
| **전체** | **96/27/17 (69%)** | **72/55/13 (51%)** |

## 핵심 결론: 칩은 성별별로 다르게 짜야 한다

문형 순위가 성별로 완전히 뒤집힌다. 여성 1등(무드 95%)이 남성에선 45%,
여성 최하위권(에스테틱 40%)이 남성에선 공동 1등(60%).

- **여성 칩 = 문형 채택 방식.** 무드·소재·핏·컬러 4개 문형이 채택 기준(S율 75%)을
  넘음 → 문형 안에서 값을 갈아끼워도 안전. 배합 권고: 무드 4 : 소재/핏 3 : 컬러 2 :
  에스테틱 화이트리스트 1.
- **남성 칩 = 값 화이트리스트 방식.** 어떤 문형도 75%를 못 넘음 → 문형 단위 신뢰
  불가, S 판정 72개 값만 개별 등록. 축은 핏/실루엣·에스테틱 중심.
  남성 에스테틱 S값: y2k baggy jeans · gorpcore jacket · ivy league oxford shirt ·
  skate baggy pants · city boy pants · normcore tee · fisherman sweater · amekaji shirt ·
  western boots · mod harrington jacket · grunge flannel shirt · indie band tee
- 남성의 특징: F가 적고(13) M이 많음(55) — "엉뚱한 걸 주진 않지만 안목이 안 보임".
  원인은 남성 인디 카탈로그가 얇은 것(ZARA가 결과 카드의 30%, 여성은 23%).
  → 칩 문제라기보다 **남성 카탈로그 확충이 선행 과제**라는 신호.

## 성별 교차 발견

1. **gender 누수 (제품 개선 후보).** burgundy cardigan·khaki shirt·red knit vest가
   여성 S → 남성 F. 남성 쿼리에 여성 상품이 섞여 나옴 — v6에서 성별은 임베딩 소프트
   신호일 뿐 하드필터가 없어서. 니트/가디건류에서 특히 심함. 남성 모드의 gender
   하드필터(또는 결과 후처리) 검토 가치 있음.
2. **상황(TPO) 대조군의 반전.** 여성 40% vs 남성 60%. 남성복은 TPO 문법(하객 수트,
   면접 슬랙스, 캠핑 자켓)이 정형화돼 있어 임베딩이 잡음. "상황 문형 전면 배제"는
   여성 기준의 결론이고, 남성은 S값(하객·면접·캠핑·운동 등 12개) 화이트리스트 가능.
3. **양성 공통 블랙리스트** (양쪽 모두 F 또는 F/M): checked jacket · houndstooth coat ·
   old money knit sweater · dark academia blazer · tweed jacket · quilted jacket.
   클래식/포멀 패턴·원단 계열 — 인디 카탈로그 공급 공백과 정확히 겹침.

## 칩 금지 값 (F 블랙리스트)

- 여성 F 17개: lavender hoodie · pleated midi skirt · drop shoulder sweatshirt ·
  elegant midi skirt · coquette blouse · old money knit · military jacket ·
  grunge flannel shirt · indie band graphic tee · striped shirt · checked jacket ·
  houndstooth coat · embroidered shirt · quilted jacket · travel bag ·
  concert outfit jacket · weekend brunch blouse
- 남성 F 13개: burgundy cardigan · khaki shirt · red knit vest · tweed jacket ·
  velvet blazer · dark academia blazer · old money knit · french chore coat ·
  checked jacket · houndstooth coat · leopard print cardigan ·
  office workwear blazer · graduation ceremony jacket

## 구조적 금지 (문형 자체가 시스템 미지원 — 성별 무관)

가격 조건("5만원 이하 ~", v6 RPC에 가격 필터 없음 — 100% 실패) · 부정형("~ 빼고") ·
카탈로그 밖 브랜드.

## 남은 것 / 다음

- [ ] 확정 문형·값 현규(기획) 전달 → 메인 홈 푸터 칩 반영 (여성=문형, 남성=화이트리스트)
- [ ] S값들을 `tests/eval/search_quality_dataset.json` 케이스로 이관 — 검색 로직 변경 시 회귀 감시
- [ ] 남성 gender 누수: 하드필터/후처리 검토 (AI 서버 백로그)
- [ ] 남성 인디 카탈로그 확충 — 남성 성적 저조의 근본 원인 (크롤러/소싱 과제)
- [ ] ZARA 편중(여 23% / 남 30%) — "Kiko 안목" 노출 정책 논의
- [ ] 2차 라운드: 색 하드필터(p_color_family) on 조건에서 컬러 문형 재검증

---

## 3차: p_subcategory 정밀 필터 전후 비교 (2026-07-15, PR #145 배포 검증)

신규 문형 2개 추가 (`p8_subcategory` 20값 자동 지표 / `p9_situation_pure` 12값
사람 판정). 러너 `--precision on|off` A/B — off 는 kill-switch 플래그로 배포 전
동작 재현. EC2 ai-server 컨테이너(실 프로덕션 코드/DB)에서 실행.

### p8 — subcat_hit (top-10 중 정답 subcategory 비율, NULL row = miss)

| 지표 | 배포 전 (off) | 배포 후 (on) |
|---|---|---|
| 평균 subcat_hit | **49.0%** | **100.0%** (20/20 쿼리 만점) |
| 0결과/결과 부족 | 없음 (전부 10건) | 없음 (전부 10건 — 완화 재시도 발동 불필요, strict 풀 충분) |
| 전 top-10 대비 신규 상품 | — | 평균 5.1/10 교체 |

최대 개선: 여름 뮬 0→100% · 와이드팬츠/청바지/크로스바디 10→100% ·
스니커즈/부츠/터틀넥 20→100%. (전 런의 실패는 실제 오염 — 뮬 쿼리에 샌들/힐,
청바지 쿼리에 팬츠류 혼입.)

주의: on 런의 100% 는 필터가 보장하는 값이라 자기증명 성격 — 이 지표의 의미는
(1) 필터가 실서버 E2E로 작동한다, (2) subcategory 60% 채움으로도 top-10 을
채울 카탈로그 깊이가 있다 (기아 현상 없음). **시각 품질**은 리포트
(`out/report_p8p9_on.html`) 사람 판정으로 확인할 것.

### p9 — 순수 상황(TPO) 베이스라인 (Phase 4 착수 전 기록)

아이템 단어 없는 순수 상황 쿼리 ("결혼식 하객룩") — family gate 단서 없음 →
cosine-only. A/B 무차이 (예상대로 — 오늘 배포는 이 문형에 영향 없음).
결과의 46%(55/119)가 subcategory 미라벨 상품. 판정 리포트로 S/M/F 매긴 뒤
Phase 4 (enhance_query 재활성 + description rerank) 전후 비교 기준으로 사용.

- [ ] p9 사람 판정 (report_p8p9_on.html) → Phase 4 baseline S율 확정
- [ ] Phase 4 구현 후 p9 재실행 → 전후 비교

---

## 4차: p_gender 하드 필터 전후 비교 (2026-07-16, PR #149 배포 검증)

2차 남성 검증에서 실측된 gender 누수(burgundy cardigan 여S→남F)에 대한 조치.
RPC 8-arg (`p_gender` — `gender && ARRAY[p_gender,'unisex']`, 전 rung 무완화)
+ 봇 3경로 구조화 gender 전달. 남성 매트릭스 p1(컬러)+p4(무드) 40쿼리 A/B.

### 객관 지표 — 여성 전용 상품 누수율 (결과 400건 × DB gender 배열 대조)

| | 배포 전 (off) | 배포 후 (on) |
|---|---|---|
| **여성 전용 상품 혼입** | **71/400 (17.8%)** | **0/400** |
| 결과 부족(기아) | 없음 (전부 10건) | 없음 (전부 10건) |

단건 프로브에서도 동일: women+cargo-pants 필터 off 시 top-20 중 10건이
men-only → on 시 위반 0. 브랜드 EXACT 필터(p_brand_names + brand_node_cache
canonical resolve)도 동시 배포 — Acne Studios 프로브 5/5.

같이 배선된 것: LLM tool args 에 `category`/`brand` 신설 (category 는
스키마에 없어 unknown_keys 로 거부되던 배선 — 순수 텍스트 턴의 family
gate/서브카테고리가 이제 실제로 작동).

- [ ] 남성 전체 문형(7개) 재검증 — 2차 S율(45~60%) 대비 gender 필터 반영
  후 개선 폭 측정 (사람 판정 필요, report_men_genderon.html)
- [ ] 남성 인디 카탈로그 확충 과제는 여전히 유효 (필터는 누수만 해결)

---

## 5차: 상황 쿼리 멀티 확장 전후 비교 (2026-07-16, PR #151 배포 검증)

p9(순수 상황/TPO) 12쿼리에 통제된 sub_queries(LLM 확장 재현값) 부여 후
`--precision off`(단일 검색 = 현행) vs `on`(멀티 확장 + 인터리브 병합) A/B.

### 자동 지표 — 결과 top-10 의 subcategory 종류 수 (코디 구성 대리 지표)

| | 단일 (off) | 멀티 (on) |
|---|---|---|
| **평균 subcategory 다양성** | **1.8종** | **3.5종** |

단일 검색은 상황 쿼리를 한 아이템 종류로 붕괴시킴 — "운동 갈 때 입을 옷"
→ tank-top만 10개, "장마철" → trench-coat만. 멀티 확장은 코디를 구성 —
운동 → shorts/sneakers/t-shirt/tank-top, 하객룩 → maxi-dress/blouse/heels/
shirt 믹스. 상황 쿼리의 본질(전체 코디 추천)에 멀티가 부합.

### 레이턴시

단일 ~1.5s → 멀티 ~6s (콜드, 3배 병렬 임베딩), 캐시 히트 시 1.5~2s. 상황
쿼리는 매번 달라 캐시 히트율이 낮을 수 있음 — 정확도 vs 레이턴시 트레이드오프.

주의: subcategory 다양성은 "코디가 구성됐나"의 대리 지표일 뿐 —
아이템들이 서로 어울리는지/상황에 맞는지 **시각 품질은 사람 판정**
(`report_p9_on.html`). 실제 봇에선 nova-lite 가 sub_queries 를 생성하므로
확장 품질은 라이브 관측 필요 (하네스는 통제값으로 병합 로직만 검증).

- [ ] p9 사람 판정 (report_p9_on.html) → 코디 적합성 S율
- [ ] 레이턴시 완화 검토 (서브쿼리 2개로 축소 / 임베딩 프리페치)
