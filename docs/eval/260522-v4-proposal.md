# V4 플로우 제안서 — 리서치 + 우선순위

> **작성일**: 2026-05-22
> **상태**: 제안 (의사결정 대기 — 한상호 검토용)
> **선행**: `260521-v3-eval-report.md` (V3 자동 평가 + P0/P1 패치 6건 §7)
> **목적**: V3 패치로 메꿔진 부분을 제외하고, V4 로 가져갈 후보를 근거·리스크·SPEC 가능성과 함께 우선순위화
> **검증 기반**: V3 자동 평가 12 시나리오 실측 + 코드 직접 확인 + 2026 리서치 (하단 Sources)

---

## 0. 한 줄 결론

V4 의 무게중심은 **"검색을 더 부르자"가 아니라 "검색 품질을 신호로 읽고, 모호성을 똑똑하게 라우팅하자"** 다. V3 평가에서 드러난 가장 큰 구조적 사실 2개:

1. **Reflexion(Gap2)은 사실상 죽은 코드** — 트리거가 `candidates_count == 0` 인데 v6 embedding-first 는 아무리 황당한 쿼리도 항상 top-15 를 반환한다 (S9: "라벤더 가죽 칠부 카프리 9XL" → 15건). 0건은 RPC 가 통째로 실패할 때만 발생. → Reflexion 은 한 번도 안 터진다.
2. **검색 품질 신호는 count 가 아니라 distance** — 리서치 consensus: top-k 는 "관련 문서가 존재함을 아는 경우"에만 안전하고, 실제로는 "top-k 받되 distance > 임계값은 sanity drop" 조합이 프로덕션 권장. v6 는 distance 를 이미 RPC 가 반환(`[STEP 4.6] distance_dist min/median/max`)하는데 **에이전트 의사결정에 전혀 안 쓰고 있다.**

---

## 1. V3 패치로 이미 해결/부분해결된 후보 (V4에서 제외)

| 원래 후보 (260521 §5) | 상태 | 비고 |
|---|---|---|
| 1. Search-First Policy 정형화 | ✅ 부분해결 (P0-2) | system prompt 에 SEARCH-FIRST 추가됨. 단 "무조건 검색"은 무딘 도구 — §2-A 에서 정교화 제안 |
| 3. Tool result self-correction | ✅ 부분해결 (P0-1) | axis reject 시 valid options 동봉. 다른 tool 로 확장 여지 |
| 4. 이미지 입력 fastpath | ✅ 해결 (P1-3) | CDN host fastpath 추가. 추가 host 발견 시 리스트만 보강 |
| 7. 관측 보강 (catalog coverage) | ✅ 부분해결 (P1-5) | evaluator_run/taste_update 배선. 잔여는 §2-D |

→ **순수 V4 신규 후보**는 아래 §2 의 6개 (A~F).

---

## 2. V4 신규 후보 (우선순위순)

### 🥇 V4-A. 검색 품질을 distance 신호로 읽기 (최우선)

**문제**: v6 는 항상 15건 반환 → 에이전트는 "결과 있음 = 성공"으로 착각. 거리가 멀어 사실상 미스인 경우(S9 라벤더 카프리)에도 그냥 카드를 쏜다. Reflexion 은 count==0 만 보므로 영영 안 터진다.

**제안**:
- `search_products` result_summary 에 **distance 통계** 노출 (`min_distance`, `median_distance`, `degraded_count`). RPC 가 이미 반환하므로 신규 계산 없음.
- 에이전트 의사결정 신호를 count → distance 로 전환:
  - `min_distance > 임계값` (예: 0.55, 캘리브레이션 필요) → "weak result" 로 간주 → `suggest_next_step` / `ask_user_clarification` 유도
  - `degraded_count` 높음 (style-node 필터 드롭多) → 검색이 카테고리-only 폴백했다는 신호 → 사용자에게 톤다운된 멘트
- **Reflexion 트리거 재정의**: `candidates_count == 0` → `min_distance > 임계값 OR candidates_count == 0`. 그래야 Gap2 가 실제로 동작.

**근거**: 리서치 — "taking the top 50 but also dropping anything with a distance above 0.3 as a sanity filter" (단 임계값은 query-dependent 라 글로벌 상수는 위험 → 캘리브레이션 데이터 필요).

**리스크**: raw cosine 임계값은 쿼리마다 의미가 달라 글로벌 상수가 부정확할 수 있음. → **선결 작업**: dev-app `ai.card_impression` + Langfuse trace 의 distance 분포를 실데이터로 모아 임계값 캘리브레이션. 임계값을 env 로 빼서 튜닝.

**SPEC 가능성**: 높음. `SPEC-SEARCH-QUALITY-SIGNAL-001`. distance 노출(작음) + 트리거 재정의(중간) + 캘리브레이션(데이터 의존).

---

### 🥈 V4-B. Reflexion 존폐 결정 (구조 정리)

**문제**: Gap2 Reflexion 은 evaluator helper 모듈 전체 + `SELF_CRITIQUE_*` + `EVALUATOR_*` env 패밀리를 살아있는 의존성으로 유지시킨다 (`_reflexion.py` 의 CROSS-SPEC LIVE DEPENDENCY 주석 참조). 그런데 트리거가 count==0 이라 **프로덕션에서 한 번도 안 터진다.** 죽은 코드가 큰 표면적을 점유.

**제안 — 택1**:
- **(B1) 살린다**: V4-A 와 묶어 distance 기반 트리거로 부활시켜 실효화. (V4-A 채택 시 자연스럽게 이쪽)
- **(B2) 죽인다**: Reflexion + evaluator helper + env 패밀리를 제거. V3 4-Gap 중 Gap2 만 드랍. 코드 표면적 대폭 축소.

**판단 포인트**: V4-A 를 한다면 B1 (Reflexion 이 distance 트리거로 의미를 가짐). V4-A 를 안 한다면 B2 (죽은 코드 제거가 정직).

**리스크**: B2 는 되돌리기 비용 있음 (SPEC-AGENT-V3-REACT 의 Gap2 회귀). B1 은 evaluator LLM 호출 비용/지연 부활 — 단 distance 기반이라 빈도는 낮음.

**SPEC 가능성**: 중간. B1 은 V4-A 에 흡수. B2 는 `SPEC-AGENT-V4-REFLEXION-RETIRE-001`.

---

### 🥉 V4-C. 모호성-인지 라우팅 (clarify vs search 컨트롤러)

**문제**: P0-2 로 "검색 먼저"를 박았지만, 리서치는 **clarifying question 이 오히려 retrieval 품질을 높인다**고 한다 (LLM-generated clarify > human-generated, 커버리지 +7~39%). 즉 "무조건 검색"도 "무조건 clarify"도 둘 다 틀림 — **모호성 정도에 따라 라우팅**해야 함.

**제안**:
- 입력 신호 수(category/color/fit/brand/style/garment) 로 ambiguity score 산출 (이미 P0-2 가 "2+ 면 검색" 이라는 거친 버전).
- V4-A 의 distance 신호와 결합: **검색 먼저 → 결과가 weak (min_distance 큼) 이면 그때 clarify**. 즉 "speculative search → distance 보고 → clarify-or-respond". 한 턴에 검색+판단을 끝냄.
- clarify 시 axis 자동 추론 (원래 후보 2): LLM 이 axis 라벨을 만들지 않고, 부족한 신호 슬롯(예: category 없음 → `category_pick`)에서 결정론적으로 axis 선택.

**근거**: 리서치 — "test alternative strategies (clarify vs rewrite vs direct) and optimize routing controllers". reward-weighted 라우팅이 search-only 보다 우수.

**리스크**: speculative search 가 검색 비용 1회 추가 (Modal embed + RPC). 단 임베딩 캐시(migration 0007)가 있어 반복 쿼리는 저렴.

**SPEC 가능성**: 중간~높음. `SPEC-AGENT-V4-ROUTING-001`. V4-A 와 강하게 결합 (distance 신호 선행 필요).

---

### V4-D. picker auto-pick (UX 턴 절약)

**문제**: 이미지에서 멀티 아이템(2~4개) detect 시 `pick_item` 이 항상 carousel 을 보내고 턴을 종료 → 사용자가 `item:N` 탭해야 검색 시작. V3 평가 S3/S4/S10/S11 전부 여기서 멈춤. 한 왕복 추가.

**제안**:
- vision 결과에 confidence/primary-item 신호가 있으면 (vision v2 schema 의 `items[0]` 이 outfit 대표) **top-1 자동 선택 + 즉시 검색**, 응답에 "이거 기준으로 찾았어 — 다른 아이템 보려면 1/2/3 눌러" fallback 키보드.
- 단일 아이템은 이미 auto (pick_item 안 거침). 멀티도 "대표 1개 auto + 나머지 옵션" 으로.

**근거**: 턴 수 = 이탈률. 패션 봇에서 "사진 → 골라 → 또 기다려"는 3-hop. 2-hop 으로 단축.

**리스크**: 자동 선택이 사용자 의도와 다를 수 있음 (상의 원했는데 신발 골림). → fallback 키보드로 즉시 교정 가능하게 하면 리스크 낮음. `pick_item_done.auto_picked=True` 로 관측.

**SPEC 가능성**: 중간. `SPEC-VISION-AUTOPICK-001`. vision v2 의 대표 아이템 선정 로직이 관건.

---

### V4-E. card:like → taste_update emit (관측 잔여, N-A)

**문제**: P1-5 로 `update_taste` tool 에는 `taste_update` emit 추가했으나, `card:like` 핸들러(`_handle_card_like` → `record_click`)는 `card_clicked` 만 emit 하고 `taste_update` 는 안 한다. taste mutation 의 절반이 관측 catalog 에서 누락.

**제안**: `_handle_card_like` 의 taste 반영 지점에 `taste_update(source="click")` emit 추가.

**리스크**: 거의 없음 (fail-soft emit). 작은 패치.

**SPEC 가능성**: 낮음 (quick-win, SPEC 불필요). V3-fix 후속으로 바로 처리 가능.

---

### V4-F. eval 러너 picker 시뮬레이션 + 실 chat_id 모드 (N-D)

**문제**: `scripts/eval/run.py` 가 mock chat_id 라 ① 카드 실도착 ② pager cursor ③ 임프레션 ④ picker→검색 끝단을 측정 못 함 (Telegram send 실패).

**제안**:
- picker 콜백(`item:N`) 시뮬레이션 추가 → 이미지→검색 happy path 끝까지 자동 측정.
- "실 chat_id 모드" 옵션 (`--real-chat <id>`) → 본인 텔레그램으로 실제 발사해 카드/cursor/임프레션까지 자동 캡처. (현재는 사람-손 검증 필요)

**리스크**: 실 chat_id 모드는 본인 폰에 카드 폭탄. opt-in 플래그로.

**SPEC 가능성**: 낮음 (테스트 인프라). SPEC 불필요.

---

## 3. 우선순위 종합

```
---
🎯 V4 후보 우선순위

[🥇] V4-A 검색 품질 distance 신호       ← 최우선. 나머지 다수가 여기에 의존
[🥈] V4-B Reflexion 존폐               ← A 와 묶어 결정 (A 하면 살리고, 안 하면 죽임)
[🥉] V4-C 모호성-인지 라우팅            ← A 선행 필요. clarify vs search 정교화
[ ] V4-D picker auto-pick             ← 독립적, UX 턴 절약. 병행 가능
[ ] V4-E card:like taste_update emit  ← quick-win, SPEC 불필요, 지금 바로 가능
[ ] V4-F eval 러너 강화               ← 테스트 인프라, 독립적
---
```

**의존 그래프**: V4-A → (V4-B 결정, V4-C 라우팅) 가 핵심 체인. V4-D/E/F 는 독립 병행 가능.

---

## 4. 추천 실행 순서 (제안)

1. **지금 바로 (SPEC 불필요 quick-win)**: V4-E (card:like emit) + V4-F (picker 시뮬레이션) — 작고 독립적, V3-fix 머지에 묶어도 됨.
2. **V4 1차 (데이터 선행)**: V4-A 의 선결 — distance 분포를 dev 실데이터로 수집해 임계값 캘리브레이션. 동시에 distance 를 result_summary 에 노출(코드 작음).
3. **V4 2차 (A 채택 후)**: V4-B 결정(A 채택 시 B1 자동) + V4-C 라우팅 정교화.
4. **V4 병행**: V4-D auto-pick (vision 대표 아이템 로직).

---

## 5. 의사결정 필요 항목 (한상호 → 나)

| # | 질문 | 선택지 |
|---|---|---|
| Q1 | V4-A (distance 신호) 가나? | 간다 / distance 노출만 먼저 / 보류 |
| Q2 | Reflexion 살릴까 죽일까? | B1 살린다(A와 묶음) / B2 죽인다(코드 정리) / 보류 |
| Q3 | V4-C 라우팅 정교화 범위? | speculative search→distance→clarify 풀구현 / ambiguity score 만 / 보류 |
| Q4 | V4-D auto-pick 가나? | 간다 / 보류 |
| Q5 | quick-win(E/F) 지금 바로 처리? | 응 V3-fix에 묶어 / 따로 / 보류 |
| Q6 | 각 항목 SPEC 으로 묶을지 / 직접 패치할지 | SPEC / 직접 / 항목마다 다름 |

---

## Sources

- [Relevance Filtering for Embedding-based Retrieval (arXiv 2408.04887)](https://arxiv.org/html/2408.04887v1) — top-k vs threshold, distance sanity filter
- [Better RAG Retrieval — Similarity with Threshold (Medium)](https://meisinlee.medium.com/better-rag-retrieval-similarity-with-threshold-a6dbb535ef9e) — "top 50 but drop distance > 0.3" 프로덕션 패턴
- [Disambiguation in Conversational QA in the Era of LLMs and Agents: A Survey (arXiv 2505.12543)](https://arxiv.org/html/2505.12543v2) — clarify vs rewrite vs direct 라우팅 컨트롤러
- [AGENT-CQ: Automatic Generation and Evaluation of Clarifying Questions (arXiv 2410.19692)](https://arxiv.org/pdf/2410.19692) — LLM-generated clarify 가 retrieval 효과 ↑
- [Modeling Future Conversation Turns to Teach LLMs to Ask Clarifying Questions (OpenReview)](https://openreview.net/forum?id=cwuSAR7EKd) — clarify 정책 학습
