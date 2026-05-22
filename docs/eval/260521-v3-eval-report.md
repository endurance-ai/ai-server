# V3 ReAct 플로우 자동 평가 리포트

> **작성일**: 2026-05-21 (KST 16:33)
> **대상**: 로컬 :8001 (DEV 봇 토큰, 영구 단일 토폴로지 — SPEC-AGENT-V2-CLEANUP-001 적용 상태)
> **검증 기준**: 12개 골든 시나리오 자동 실행 + PG/Redis/Langfuse 관측
> **러너**: `scripts/eval/run.py` (webhook 합성 POST, chat_id=999999999 격리)
> **원본 결과**: `/tmp/v3_eval_results.json`, `/tmp/v3_eval_full.log`
> **재검증**: 2026-05-22 (P0/P1 패치 6건 적용 후) — §7 참조

---

## 0. TL;DR

V3 의 **기본 그래프 토폴로지·관측 인프라·EN sticky·empty result 응답 UX 는 정상 동작**한다. 그러나 **에이전트 정책 + tool contract 정합성에서 P0 결함 2건이 모든 텍스트 쿼리 시나리오를 망치고 있다.**

```
---
🎯 V3 골든 시나리오 12개 결과 — 종합

[🟢] S1  · /start 신규 유저 인트로            ← PASS · 3 events, intro 노드 1턴, onboarded_at 기록
[🔴] S2  · 한글 텍스트 쿼리 happy path        ← FAIL · 검색 0건, ask_clarify axis invalid 무한 반복
[🔴] S3  · Unsplash 이미지 URL                ← FAIL · link_resolved=None, vision 미실행, pending Q 누설
[🟡] S4  · weak vision → clarify cards         ← INCONCL · vision 실패로 텍스트 폴백 (분석 가능 신호 없음)
[🟡] S5  · cards:refine 버튼                  ← INCONCL · S2 의 last_results 없어 의미 없음
[🟡] S6  · cards:more 페이저                   ← INCONCL · cursor None, S2 실패의 연쇄
[🟡] S7  · card:like → taste profile           ← INCONCL · last_results 없어 like target 미존재
[🟢] S8  · EN sticky                           ← PASS · 영어 sticky 유지, search_products × 2, suggest_next_step 호출 확인
[🟢] S9  · 빈 결과 → Reflexion (Gap2)          ← PASS (UX) · 사과+대안 메시지 우아함. 단 evaluator_run 이벤트 부재 (관측만 누락)
[🔴] S10 · Pinterest 링크                     ← PARTIAL · vision 4 items 정상, pick_item 카드 발사로 awaiting_item_pick 정지
[🔴] S11 · IG 포스트 링크                     ← PARTIAL · 동상 (Apify 정상, vision 정상, pick 대기)
[⏸️] S12 · Redis down fail-open               ← 측정 실패 (러너 버그 — post-scenario probe 가 Redis 시도)
---
```

**3분 안에 핵심을 묻는다면**:
- [🔴 P0] `ask_user_clarification` tool 의 axis whitelist ↔ LLM 이 보내는 axis 이름 사이 미스매치. 모든 텍스트 쿼리에서 fail-fast.
- [🔴 P0] 에이전트가 충분한 정보 (color/fit/subcategory) 있어도 **검색을 먼저 안 하고 clarify 부터 호출** — 시스템 프롬프트 우선순위 문제.
- [🟡 P1] `resolve_image` 가 Unsplash CDN URL 을 해석 못 함 (`resolved_image_url=None`). Vision 스킵 후 폴백이 직전 turn 의 pending_question 을 흘림.
- [🟡 P1] Pinterest/IG vision 은 잘 되는데 pick_item 정지가 정량 happy-path 측정을 어렵게 함.
- [🟢] EN sticky / first-touch / 빈 결과 우아한 사과 / 임프레션-Redis 분리 / Langfuse trace 결연결 — 전부 작동.

---

## 1. 환경 & 측정 인프라

| 항목 | 값 |
|---|---|
| 서버 | 로컬 uvicorn `:8001` (WatchFiles reload on) |
| 봇 토큰 | DEV `kiko_fashion_ai_bot` (실제 Telegram 전송은 chat_id=999999999 라 "chat not found" 로 401, 내부 흐름은 정상) |
| 채팅 격리 | `chat_id=999999999` / `user_id=999999999` 단일 ID 로 시나리오 12개 직렬 실행 |
| PG | dev-app `54.116.104.193:5432/kikoai`, schema `ai.*` |
| Redis | 로컬 `redis://localhost:6379/1`, prefix `kiko:*` |
| Modal | `MODAL_EMBED_URL` (FashionSigLIP), 실호출 |
| Langfuse | dev-ai self-host v3, 실 trace 발생 |
| 측정 소스 | `ai.log_conversation_event`, `ai.user_session`, `ai.user_taste_profile`, `ai.card_impression`, redis `kiko:cursor:*` / `kiko:imp:*` |

---

## 2. 시나리오별 채점

### S1 · `/start` 신규 유저 인트로 — 🟢 PASS

| | 기대 | 관측 |
|---|---|---|
| 이벤트 | user_text + intent_routed + bot_text(intro) | ✅ 3건 매칭 |
| 노드 | ingest → intro → END | ✅ (agent 미진입) |
| onboarded_at | 신규 → set | ✅ 16:26:52 set |
| 검색/카드/임프레션 | 0 | ✅ 0 |
| 응답 언어 | KO | ⚠️ `lang_detected="en"` (영어로 판정 — `/start` 가 한글 아님) — 정상이나 추후 `/start` 같은 슬래시 명령은 lang detect 스킵 고려 |

### S2 · 한글 텍스트 쿼리 happy path — 🔴 FAIL

**입력**: `검정 오버사이즈 후드 추천해줘`

**관측**:
| iter | tool | args | result |
|---|---|---|---|
| 1 | ask_user_clarification | `axis="gender"`, prompt=`"누가 입을 거야?"`, options=list[1] | `ok=False, error="invalid_axis:gender"` |
| 2 | ask_user_clarification | `axis="gender"` (재시도) | `ok=False, error="invalid_axis:gender"` |
| 3 | respond | text=`"검정 오버사이즈 후드 여자 거 찾는 거야, 남자 거야, 아니면 상관없이?"` | ok=True |

**결함**:
- 🔴 `gender` 는 valid axis 가 아님. 코드 ground-truth (`app/channels/clarify.py`, `clarify_values.py`) 의 axis set: `category_pick / formality / fit / occasion / subcategory_disambiguation / generic_fallback`.
- 🔴 LLM 시스템 프롬프트에 valid axes enum 이 누락 되어 있어 LLM이 자기 머릿속에서 axis 이름을 만듦 (gender / wearer / occasion & vibe).
- 🔴 정작 입력에 검색에 충분한 정보 (color=black, fit=oversized, category=hoodie) 가 있는데도 `search_products` 호출 자체를 안 시도. 시스템 프롬프트가 "필요한 정보 부족시 clarify" 보다 "기본 clarify 부터" 정책.
- 결과: 사용자는 카드 UI 없이 텍스트로만 질문 받음 → SPEC-CLARIFY-CARDS-001 의 UX 후퇴.

### S3 · Unsplash 이미지 URL — 🔴 FAIL

**입력**: `https://images.unsplash.com/photo-1542272604-787c3835535d?w=800` (URL 엔티티)

**관측**:
- `link_resolved`: host=`images.unsplash.com`, **resolved_image_url=None** 🔴
- vision_done **없음** (vision 노드 미진입)
- 응답: S2 의 동일 질문 그대로 ("검정 오버사이즈 후드 여자 거..." — pending_question 누설)

**결함**:
- 🔴 `resolve_image` 노드가 Unsplash CDN URL 을 og:image 파싱 후보로만 보고 직접 이미지로 안 잡음. Unsplash 의 photo URL 은 page 가 아니라 이미 raw image. 정상 흐름이라면 URL 자체를 직접 vision 으로 던져야 함.
- 🔴 link 해석 실패 → graceful degrade 가 "비전 스킵 후 직전 pending_question 재발사" 라는 **크로스턴 상태 누설** 로 빠짐. `pending_question.py` 의 lifecycle 이 image-input turn 에서 clear 안 됨.

### S4 · weak vision → clarify cards — 🟡 INCONCLUSIVE

**입력**: 다중 아이템 풀바디 사진 (Unsplash URL)

**관측**:
- link_resolved 정상 (이번 URL 은 resolve_image 가 통과시킴)
- `analyze_image` tool 호출 (iter 1) → respond
- 응답: "아, 사진이 제대로 안 들어왔네! 다시 한 번 사진 올려줄래?" — Vision 실패 경로

**판단**: vision 자체가 실패해 clarify-card 시나리오 측정 불가. ask_clarify 노드 도달 못함. 따로 합성 vision payload 로 weak-vision 강제하는 별도 테스트 필요.

### S5 · cards:refine 버튼 — 🟡 INCONCLUSIVE

S2 가 last_results 를 채우지 못해서 refine 할 대상이 없음 → 봇이 "사진이 제대로 안 들어왔던 거 같은데, 지금 뭘 찾고 있어?" 로 빈손 응답. S2 가 PASS 되어야 의미 있는 측정.

### S6 · cards:more 페이저 — 🟡 INCONCLUSIVE

`kiko:cursor:999999999` = None (이전에 한 번도 cards delivered 안 됨). cards:more 가 ingest 인라인에서 처리되며 last_results 비어 있어 no-op. **Redis cursor 자체 동작은 측정 불가**.

### S7 · card:like → taste profile — 🟡 INCONCLUSIVE

last_results 비어 있어 like target product_id 가 placeholder "0". 후속 한글 텍스트 ("비슷한 스타일로 더 추천해줘") 가 들어가도 `get_recent_history` 만 호출하고 끝남. `user_taste_profile` 로 INSERT 발생 안 함.

### S8 · EN sticky — 🟢 PASS

**입력 시퀀스**: `recommend me a cozy beige knit` → (응답 후) `cards:refine` 콜백

**관측 (12 events)**:
| iter | tool | 비고 |
|---|---|---|
| 1 | search_products | 첫 검색 시도 |
| 2 | search_products | 재시도 |
| 3 | suggest_next_step | **🟢 Gap3 proactive 작동 확인** |
| 4 | respond | "Oops, the search is being finicky right now!" — 검색 실패 사과 |
| 5 | respond | "I see you want to refine the search! Let me try a fresh approach" — refine 콜백 응답 |

**판정**:
- 🟢 EN sticky **OK**: 영어로 시작 → 콜백(텍스트 없음) 후에도 영어로 응답.
- 🟢 Gap3 proactive (`suggest_next_step`) 호출 확인됨.
- 🔴 단, 검색이 두 번 다 실패함 ("the search is being finicky") — Modal cold-start 또는 text-embed pipeline_failed (S9 와 동일 원인 가능성).

### S9 · 빈 결과 → Reflexion (Gap2) — 🟢 PASS (UX) / 🟡 PARTIAL (관측)

**입력**: `라벤더색 가죽 칠부 카프리 9XL 사이즈로 추천` (말도 안 되는 조합)

**관측**:
| iter | tool | 결과 |
|---|---|---|
| 1 | ask_user_clarification | `invalid_axis:"occasion & vibe"` 🔴 |
| 2 | ask_user_clarification | `invalid_axis:"wearer"` 🔴 |
| 3 | search_products | `text_query="lavender leather cropped capri women"`, `pipeline_failed:HTTPStatusError`, candidates_count=0 🔴 |
| 4 | respond | "라벤더 가죽 카프리가 정확히 매칭되는 게 없네 — 카탈로그가 여성 위주라 9XL 오버사이즈 사이즈도 제한적이야. 라벤더 가죽 팬츠나 칠부 바지로 범위 넓혀볼까..." 🟢 |

**판정**:
- 🟢 사용자 향 UX 응답은 우수 — 빈 결과를 "왜 안 나왔는지(카탈로그 한계)" 설명하면서 대안(카테고리 확장 / 색상 변경) 2개 제시.
- 🟡 Gap2 Reflexion 의 `evaluator_run` 이벤트 catalog 에 정의되어 있으나 **emit 되지 않음**. `app/agents/_reflexion.py` 가 별도 conversation_event 를 안 쏨 → 빈결과시 Reflexion 발동 여부를 PG로는 확인 불가. 서버 로그의 `🔬 v3:reflexion` 라인을 확인해야만 알 수 있음 — 관측성 갭.
- 🔴 search_products 가 `pipeline_failed:HTTPStatusError` 로 떨어진 정확한 원인 미상 — Modal `/embed/text` cold start 또는 PostgREST RPC 실패. S8 의 finicky 와 같은 패턴.

### S10 · Pinterest 링크 — 🔴 PARTIAL

**입력**: `https://pin.it/6W7EzMZdT` (사용자 제공)

**관측**:
- 🟢 `pinterest_ingest` event emitted (`mode="pin"`, pin_count=1) — `kiko-ai` CLAUDE.md 의 "pinterest_ingest DEAD" 메모는 부분 stale (적어도 이 핀 형태에선 emit 됨)
- 🟢 `vision_done`: `style="Street Casual"`, gender=`male`, **items 4개** — `[(shirt, BLUE), (t-shirt, WHITE), (shorts, BLACK), (loafers, BLACK)]`
- 🟢 `pick_item_done`: `auto_picked=False`, `picked_idx=-1`, `n_cands=4` — 사용자에게 picker 카드 발사 후 종료
- 🔴 그 다음 진행 없음 — `state=awaiting_item_pick` 으로 멈춤. 후속 `item:0` 콜백을 보내야 검색까지 도달

**판정**: vision 까지는 PASS. 검색까지의 happy path 는 미측정 — picker 콜백을 시뮬레이션해야 함. **실서비스 UX 자체는 정상**.

### S11 · Instagram 포스트 — 🔴 PARTIAL

**입력**: `https://www.instagram.com/p/DTdKnfvE58w/` (사용자 제공)

**관측**:
- 🟢 `link_resolved`: `https://scontent-ord5-1.cdninstagram.com/v/...615927846_18309714901250...` — Apify 경유 IG CDN 추출 성공
- 🟢 `vision_done`: `style="Street Casual"`, gender=`male`, items 4개 (denim-jacket/t-shirt/jeans/hat 모두 GREY 톤)
- 🟢 `pick_item_done`: 동일 (picker 발사 후 awaiting)

**판정**: Apify 호출 정상 (402 cooloff 미발생), vision 4 아이템 추출 OK. S10 과 같은 picker 정지.

### S12 · Redis down fail-open — ⏸️ 측정 실패 (러너 결함)

`docker stop redis-local` 후 시나리오 실행 → 그러나 시나리오 종료 후 **러너의 `get_redis_state()` 가 다시 Redis 연결 시도 → ConnectionError 로 시나리오 결과 자체가 망실**.

러너 수정 필요: post-scenario probe 를 try/except 로 감싸거나, Redis 재기동을 시나리오 본체 종료 직후로 옮겨야 함.

---

## 3. 횡단 결함 분석 (축 A~F)

### A. 그래프 흐름 정합성 — 대부분 정상

- ✅ ingest → 다음 노드 라우팅 (text/url/photo/callback) 모두 의도대로
- ✅ `intent_routed` 이벤트가 모든 시나리오에서 1개 이상 emit
- ✅ Langfuse trace id (예: `6e5916e2955239f3bffd4923f6b32c01`) 가 user_text intake 이후 모든 event 에 첨부됨
- ⚠️ S3 의 `resolve_image` 실패 시 graceful degrade 가 직전 turn 의 pending_question 발사로 빠짐 — 상태 누설 버그

### B. ReAct 루프 효율

- ✅ iter cap (`AGENT_MAX_ITERATIONS=6`) 안에서 모든 시나리오 종결, infinite-loop guard 트리거 없음
- 🔴 **iter 낭비**: S2 는 2회의 `invalid_axis` 실패 후 LLM이 plain text 로 우회. 같은 axis 를 1번 더 시도 → 동일 실패 → 결국 검색은 단 한 번도 안 함. iter 3개 중 2개가 무가치.
- 🔴 `validate_args` 의 enum 검사가 fail 만 시키고 LLM 에게 valid axes 를 다시 알려주지 않음 — re-try 가능성 차단

### C. V3 4-Gap 효과성

| Gap | 검증 | 결과 |
|---|---|---|
| Gap1 (memory injection 🧠) | 로그 `🧠 [v3:memory] injected` 라인 (서버 콘솔에서 관측). conversation_event 별도 emit 없음 | ✅ 작동, 단 관측성 갭 |
| Gap2 (Reflexion 🔬) | S9 의 빈결과 시 발동했어야 함. `evaluator_run` 이벤트 0건 | 🟡 발동 여부 미확인 (관측성 갭) |
| Gap3 (proactive 💡) | S8 에서 `suggest_next_step` tool 1회 호출 확인 | ✅ 작동 |
| Gap4 (dislike discount 🚫) | 측정 안 함 (taste_profile 비어 있어서 trigger 자체 안 발생) | — |

### D. 검색/추천 품질

**검색이 단 한 번도 정상 발사된 적이 없음** — 핵심 문제:
- S2, S3, S4, S7: 검색 미시도
- S5, S6: cascading 실패 (S2 영향)
- S8: 시도했으나 두 번 다 fail ("finicky")
- S9: 시도했으나 `pipeline_failed:HTTPStatusError`
- S10, S11: picker 단계에서 정지

→ **diversify_done / search_done(top_k_product_ids) / card_sent / card_impression 모두 0건** — V3 검색 정확도/다양성/페이저는 이번 자동 평가에서 실측 불가.

별도 직접 텔레그램 입력으로 (LLM이 clarify 강박 없이) 검색 흘릴 수 있는지 사람 손 검증 1회 권장.

### E. UX P0

| 항목 | 결과 |
|---|---|
| 사전 안내 멘트 (pre_message) firing | 🟡 vision key 는 S3/S4 에서 안 보임 (vision 미실행). search key 는 S2/S9 에서 안 보임 (search 미발사). pinterest key 는 design 상 없음. **firing site 자체는 코드 상 존재하나 실 트리거 없음** |
| typing indicator | 🟡 _fire_typing fire-and-forget 이므로 events 에 안 남음. 실제 동작은 telegram 어댑터 로그로만 확인 (chat_id 가 가짜라 Telegram이 reject → False, 그래도 graph 진행) |
| KO/EN sticky | 🟢 S2 (KO), S8 (EN) 둘 다 응답 언어 정상 |
| hybrid 카드 + 임프레션 | ❌ card_impression 0건 (검색 결과 자체가 없어서) |

### F. 관측성 자체 신뢰도

- 🟢 `langfuse_trace` 가 모든 graph-internal event 에 attach (user_text/user_photo 같은 intake event 는 trace context 진입 전이라 null — 정상)
- 🟢 conversation_event 누락 없음, thread_id 일관성 OK
- 🔴 **evaluator_run / taste_update / search_done 이벤트가 catalog 에는 있는데 실제 발생 0건** — Reflexion / taste mutation / search step 의 emit 누락 의심
  - `app/observability/event_payloads.py` 의 catalog 와 실 emit 사이트가 sync 안 됨
  - dev-ai PROD 에서도 같을 가능성 → 보고서 직후 dev-ai 실데이터 확인 권장

---

## 4. V3 핵심 결함 우선순위 (Critical → Quick-win → Nice-to-have)

### 🔴 Critical (P0 — V3 패치 필요)

#### V3-P0-1. `ask_user_clarification` axis whitelist 위반

- **현상**: LLM 이 `gender / wearer / occasion & vibe` 같은 자유 문자열 axis 를 보내고, validator 가 `invalid_axis:*` 로 reject 한 뒤 LLM 이 plain text 로 우회 — clarify 카드 UI 가 한 번도 안 생김
- **원인**: 시스템 프롬프트에 valid axes enum 이 누락되어 있음. tool description 만으로는 LLM 이 추측해버림
- **수정 방향**:
  1. 시스템 프롬프트 (또는 tool description) 에 `axis: Literal["category_pick","formality","fit","occasion","subcategory_disambiguation","generic_fallback"]` 명시
  2. `validate_args` 가 reject 시 valid options 리스트를 result_summary 에 함께 반환 → 다음 iter LLM 이 자동 보정
- **영향 시나리오**: S2, S3, S9 (모든 텍스트/이미지 입력)

#### V3-P0-2. 에이전트가 검색을 먼저 안 함 (over-clarification)

- **현상**: "검정 오버사이즈 후드" 같이 검색에 충분한 키워드가 있는데도 clarify 먼저
- **원인**: 시스템 프롬프트에 "검색을 먼저 시도하고, 결과가 빈약하거나 ambiguous 할 때만 clarify" 우선순위 없음. 또는 Gap1 memory 가 taste_profile 부재 시 "더 물어봐" 시그널을 강하게 줌
- **수정 방향**:
  1. `app/agents/react_loop.py` 의 system prompt 에 "**SEARCH-FIRST POLICY: 검색 가능한 키워드 (color/category/fit/style) 가 1개 이상 있으면 무조건 search_products 부터.** clarify 는 검색 결과 < 5 개거나 매우 모호할 때만." 추가
  2. `_PROACTIVE_DIRECTIVE` (Gap3) 와 별도로 search-first directive 추가
- **영향 시나리오**: S2, S9 (텍스트 쿼리 전부)

### 🟡 Important (P1 — V3 패치 권장)

#### V3-P1-3. `resolve_image` 가 Unsplash CDN URL 직패스 실패

- **현상**: `images.unsplash.com/photo-*` URL → `resolved_image_url=None`
- **원인**: og:image 파싱 시도 후 None 반환. raw image URL 인지 판별 후 그대로 통과시키는 fastpath 누락
- **수정 방향**: `app/channels/link_resolver.py` 에 known image host (unsplash CDN, IG CDN, Pinterest CDN) → URL 자체 반환 fastpath
- **영향**: 이미지 URL 입력의 정상화

#### V3-P1-4. pending_question 크로스턴 누설

- **현상**: S2 의 "성별 물어봄" 이 S3 에서 (이미지 입력인데도) 그대로 흘러나옴
- **원인**: `app/agents/pending_question.py` 의 lifecycle 이 image/url turn 에서 clear 되지 않음
- **수정 방향**: ingest 또는 resolve_image 진입 시 pending_question 무조건 clear (텍스트 답변이 아닌 새 입력은 이전 질문에 대한 답 아님)

#### V3-P1-5. Reflexion / taste_update 이벤트 emit 누락

- **현상**: catalog 에 정의된 `evaluator_run` / `taste_update` 가 실제로는 한 번도 emit 안 됨
- **원인**: `_reflexion.py` / taste_profile_pg upsert 사이트가 별도 conversation_event 안 쏨
- **수정 방향**: 두 사이트에 `emit(event_type=..., ...)` 추가 — 관측성 회복

#### V3-P1-6. search_products `pipeline_failed:HTTPStatusError` 산발 발생

- **현상**: S8 두 번, S9 한 번
- **원인**: Modal `/embed/text` cold-start 타임아웃? PostgREST RPC 일시 실패? 정확한 status code/원인 미상
- **수정 방향**: `pipeline_failed:` 에 underlying status_code 포함, `app/services/search_service.py` 의 retry 정책 검토

### 🟢 Nice-to-have (V4 후보)

- N-1: `pinterest_ingest` event catalog DEAD 표기 (CLAUDE.md) 와 실제 emit 사이트 정리
- N-2: pick_item 카드 발사 시 `card_sent` (turn_no=N) 도 emit 해서 picker 단계 측정 가능하게
- N-3: 사전 안내 멘트 firing 도 emit (`pre_message_fired` 등) — UX P0 의 4 site 측정 가능
- N-4: `/start` 슬래시 명령에서는 `detect_lang` 스킵하고 user_session.lang 유지

### 🔧 Eval 러너 자체 결함

- S12 측정 실패: `scripts/eval/run.py` 의 `get_redis_state()` 를 try/except 으로 감싸고, Redis 재기동을 시나리오 본체 종료 직후 (probe 전) 로 옮길 것

---

## 5. V4 방향 — 리서치 후보 (브리핑용)

위 P0/P1 픽스 후의 V4 로 가져갈 만한 후보들. **확정 아님 — V3 패치 머지 후 합의 필요.**

1. **Search-First Agent Policy 정형화** (V3-P0-2 의 영구화)
   - 시스템 프롬프트에 의사결정 트리 명시: "텍스트 → search → (결과 < 3 OR ambiguous) → clarify". JSON schema constrained tool use 로 LLM 이탈 봉쇄.

2. **Clarify Axis 자동 추론**
   - LLM 이 axis 이름을 만들지 않고, 후속 user 답변에서 axis 를 후처리 추론. 즉 `ask_user_clarification` 의 axis 가 "옵션 라벨 기반" 으로 자동 매핑.

3. **Tool result self-correction**
   - `validate_args` reject 시 result_summary 에 valid options 동봉 → 다음 iter LLM 이 자동 보정. 현 V3 는 reject = 무의미한 iter 낭비.

4. **이미지 입력 fastpath**
   - 알려진 CDN (Unsplash/Pinterest/IG/Twitter) URL 은 link_resolver 우회 → vision 직행.
   - resolve_image 실패시 graceful UX (이미지 다시 보내달라) — 현재 pending_question 누설 패턴 제거.

5. **Reflexion → 검색 자동 재시도 회로**
   - 현재 `_reflexion.py` 가 evaluator dict 만 리턴. 다음 단계: reflexion 결과를 `search_products` re-call args 에 자동 주입. 빈결과 → 필터 drop → 재검색 → 응답 (사용자가 모르게).

6. **picker (pick_item) → 자동 검색 트리거 옵션**
   - 4 아이템 detect 시, top-1 confidence 기반 auto-pick + fallback "다른 아이템 보고 싶으면 1/2/3/4" — 한 턴 절약.

7. **관측 보강: 이벤트 catalog full coverage**
   - evaluator_run / taste_update / pre_message_fired / pick_card_sent — 전부 conversation_event 로 흘려서 LLM-as-judge 정량 분석 가능하게.

---

## 6. 다음 액션 권장

| # | 액션 | 우선순위 |
|---|---|---|
| 1 | V3-P0-1 axis whitelist 시스템 프롬프트 픽스 | 🔴 Critical |
| 2 | V3-P0-2 search-first policy 시스템 프롬프트 픽스 | 🔴 Critical |
| 3 | 두 픽스 적용 후 S2/S5/S6/S7 재실행 → 검색 happy path 측정 (이번 평가 미수행 부분) | 🔴 |
| 4 | V3-P1-3 Unsplash CDN fastpath | 🟡 |
| 5 | V3-P1-4 pending_question clear on non-text turn | 🟡 |
| 6 | V3-P1-5 evaluator_run / taste_update emit 추가 (관측성) | 🟡 |
| 7 | V3-P1-6 search_products HTTPStatusError 원인 추적 (`pipeline_failed:` 상세화) | 🟡 |
| 8 | scripts/eval/run.py S12 러너 결함 픽스 | 🟢 |
| 9 | V4 후보 1~7 중 합의 후 SPEC 도출 | — |

---

**부록 A — Raw 데이터**

- 시나리오별 events 전체: `/tmp/v3_eval_results.json`
- 실행 로그: `/tmp/v3_eval_full.log`, `/tmp/v3_eval_run.log`
- 테스트 chat_id `999999999` 의 데이터는 평가 종료 후 cleanup (Task #7) 으로 제거 예정

---

## 7. 패치 후 재검증 (2026-05-22)

§4 의 Critical 2건 + Important 4건 패치를 적용하고 동일 12 시나리오를 재실행했다. **선결 조건이었던 Modal embedding endpoint 다운 상태가 해소된 후** 측정 (이전 평가 시점에는 Modal `/embed/text` 가 404 라 모든 검색이 0건이었음 — V3 결함이 아닌 인프라 다운이었고, P1-6 의 에러 상세화 덕분에 `pipeline_failed:HTTPStatusError:404@...modal.run` 으로 즉시 식별됨).

### 적용 패치 (8 파일)

| ID | 파일 | 변경 |
|---|---|---|
| P0-1 | `app/agents/tool_registry.py` | `validate_args` 에 `ask_user_clarification.axis` Literal 값 검증 추가 — reject 메시지에 valid 6-axis 리스트 동봉 |
| P0-1 | `app/agents/tool_registry.py` | tool description 에 6개 valid axis 명시 + "gender/wearer/mood 등 발명 금지" |
| P0-1 | `app/agents/tools/ask_user_clarification.py` | dispatch reject 시 valid axes 리스트 포함 (belt-and-braces) |
| P0-2 | `app/agents/react_loop.py` | system prompt 의 "Required slots(category+gender)" 블록을 **SEARCH-FIRST POLICY** 로 교체 — 키워드 2+ 면 무조건 search 먼저, gender 는 blocker 아님 |
| P1-3 | `app/channels/link_resolver.py` | `_DIRECT_IMAGE_HOSTS` fastpath — Unsplash/Pinterest CDN/IG CDN/Twitter 등은 og:image 스크랩 우회, URL 직패스 |
| P1-4 | `app/graphs/nodes/ingest.py` | image/url/callback turn 진입 시 `pending_question` 무조건 clear (텍스트 답변만 carry) |
| P1-5 | `app/agents/react_loop.py` | `_maybe_reflexion` 에 `evaluator_run` event emit |
| P1-5 | `app/agents/tools/update_taste.py` | update_taste tool 에 `taste_update` event emit |
| P1-6 | `app/agents/tools/search_products.py` / `refine_search.py` | `pipeline_failed:` 에 status_code + host 동봉 |

### Before → After 비교

| 시나리오 | Before (2026-05-21) | After (2026-05-22) | 판정 |
|---|---|---|---|
| **S2** 텍스트 쿼리 | `ask_clarification×2 (invalid_axis:gender)` → 검색 0 | `search_products(text_query='black oversized hoodie women')=15` → respond "15개 찾았어!" | 🟢 FIXED |
| **S3** Unsplash URL | `link_resolved=None` → vision 미실행 → pending Q 누설 | `link_resolved=Unsplash URL` → `vision_done(Heritage Denim, 2 items)` → pick_item | 🟢 FIXED |
| **S4** 멀티 아이템 | vision 실패 ("사진 안 들어왔네") | `vision_done(Romantic Editorial, 2 items)` → pick_item | 🟢 FIXED |
| **S5** refine | "사진이 안 들어왔던 거 같은데" (cascading 실패) | get_recent_history → respond (맥락 질문, last_results=15 보존) | 🟢 정상 |
| **S6** cards:more | cursor None (S2 실패 연쇄) | ingest 인라인 처리 (cursor 는 mock chat_id 라 send 실패 → 미advance, 실서비스선 정상) | 🟡 mock 한계 |
| **S7** card:like | last_results 없어 측정 불가 | **`card_clicked` event emit** ✅ (taste 신호 기록) | 🟢 정상 |
| **S8** EN sticky | search 두 번 실패 ("finicky") | `search_products=15` → respond, **EN 유지** ("I see you've tapped the refine button!") | 🟢 정상 |
| **S9** 빈 결과 | search HTTPStatusError | `search_products=15` (v6 는 cosine-nearest 15 항상 반환) → "니치한 조합이라... 대신 라벤더 톤 크롭 팬츠 골라봤어" | 🟢 정상 |
| **S10** Pinterest | vision OK, pick 정지 | `vision_done(Young Casual, 4 items)` → pick_item (동일) | 🟢 정상 |
| **S11** IG | vision OK, pick 정지 | `vision_done(Street Minimal, 4 items)` → pick_item (동일) | 🟢 정상 |
| **S12** Redis down | 러너 크래시 (측정 실패) | **`redis=unreachable` 인데 search_products=15 → respond 정상 발사** (fail-open 검증) | 🟢 FIXED |

### 핵심 검증 포인트

1. **P0-1 axis whitelist**: S12 에서 LLM 이 `axis='fit'` (valid) 사용 — 패치 전 `gender`/`wearer`/`occasion & vibe` 같은 invalid axis 발명이 사라짐. (해당 clarify 는 옵션 1개라 send 실패했으나 곧바로 search_products 로 self-correct.)
2. **P0-2 search-first**: S2/S8/S9/S12 전부 clarify 가 아닌 `search_products` 를 먼저 호출. text_query 도 canonical form (`black oversized hoodie women`) 정확.
3. **P1-3 CDN fastpath**: S3 (Unsplash) `resolved_image_url` 이 URL 자체로 통과 → vision 정상.
4. **P1-4 pending clear**: S3 에서 직전 turn 질문 누설 사라짐.
5. **P1-5 emit**: `card_clicked` (S7), `taste_update`/`evaluator_run` 사이트 배선 완료. 단 `evaluator_run` 은 v6 가 항상 cosine-nearest 15 를 반환해 "candidates<3 → Reflexion" 조건이 자연 발생하지 않아 이번 런에서 트리거 안 됨 (배선은 정상, 트리거 조건 미충족).
6. **P1-6 에러 상세화**: Modal 404 를 `pipeline_failed:HTTPStatusError:404@...modal.run` 으로 즉시 식별 — **이 패치가 인프라 다운을 V3 결함과 분리해준 결정적 도구**.

### 잔여 메모 (V4 트랙 후보)

- **N-A**: `card:like` 핸들러(`_handle_card_like`)는 `record_click` 경로라 `taste_update` emit 미발생 — `card_clicked` 만 emit. taste_update 까지 원하면 핸들러에도 배선 필요.
- **N-B**: `evaluator_run` 실 트리거 검증을 하려면 candidates<3 강제 시나리오 필요 (v6 embedding-first 는 거의 항상 15 반환). Reflexion 의 실효성 자체를 재고할 여지.
- **N-C**: S6 pager cursor / 임프레션은 mock chat_id 라 Telegram send 가 실패해 측정 불가 — 실 chat_id 1회 사람-손 검증 권장.
- **N-D**: pick_item 정지(S3/S4/S10/S11)는 정상 UX 이나, picker 콜백(`item:N`) 시뮬레이션을 러너에 추가하면 이미지→검색 happy path 끝까지 자동 측정 가능.

---

## 8. 실 텔레그램 수동 테스트 발견 + 추가 패치 (2026-05-22)

§7 패치 후 한상호 본인이 DEV 봇(@kiko_dev_bot)에 실제 입력하며 검증. mock 으로 못 본 부분이 드러남.

### 실 테스트 결과

| 테스트 | 결과 |
|---|---|
| 텍스트 검색 (성별 안 물음) | 🟢 카드 5장 앨범 단일 버블 + 버튼 정상 |
| 더보기 | 🟡 5장 잘 나오나 **1개씩 따로** 나옴 (묶이지 않음) |
| ❤️ 토스트 | 🟢 정상 |
| 사진 → picker → 검색 | 🟢 결과 나옴, 단 **느림** |
| 영어 | 🟢 정상 |

### 발견 #1 — 더보기 카드가 1개씩 (WEBPAGE_CURL_FAILED 원자성)

로그(`15:07:43`): `sendMediaGroup ❌ 400 desc="failed to send message #3 ... WEBPAGE_CURL_FAILED"`. sendMediaGroup 은 ATOMIC — 5장 중 1장(상품 이미지 URL #3)을 Telegram 이 못 가져오면 전체 그룹 실패 → 기존엔 per-card 1-by-1 폴백으로 떨어짐. 첫 배치/영어 배치는 `ok=True n=5` 로 정상 그룹.

**패치**: `TelegramAdapter.send_media_group` — `WEBPAGE_CURL_FAILED message #N` 파싱 → 해당 아이템만 DROP 후 그룹 재시도 (≥2장 남는 한). 단일 버블 앨범 유지. `_post(return_error=True)` 옵션 추가로 에러 description 노출 (기존 호출 무영향). bool 반환 유지 → 기존 18 테스트 무손상.

### 발견 #2 — 이미지 흐름 지연 (Vision 11s + Modal cold start)

로그 타이밍 분해: resolve_image 2s → **Vision(nova-lite) 11.3s** → picker → (사용자 픽) → 검색 embed **Modal cold start ~20s (cache miss)** → RPC <1s. 한상호 가설("임베딩 단계는 동일") 절반 정확 — embed 단계는 동일, **차이는 텍스트 흐름엔 없는 Vision LLM 11s**. Modal cold start 는 양쪽 공통(cache hit 면 즉시).

**조치**: 원인 규명만 (최적화는 V4 — Vision 모델/병렬 워밍업 후보). 패치 없음.

### 발견 #3 — 🐛 성별 뒤집힘 (남성 → women) — 실은 더 심각

로그(`15:09:30`): Vision 이 감지한 picker 아이템 = "릴렉스드 네이비 블루 코튼 반바지 **남성**", 그런데 실제 검색 = `text_query='navy relaxed shorts **women**'`. **남성으로 감지된 걸 women 으로 뒤집어 검색.** 두 원인:
1. `react_loop._item_attrs` 가 한국어 대화 시 한국어 `searchQueryKo`("...남성")를 suggested_query 로 넘김 → text_query 는 영어여야 하니 LLM 이 번역하며 성별 손실/플립.
2. 시스템 프롬프트의 "catalog is women-leaning" 문구가 Haiku 를 women 쪽으로 편향. genderless 쿼리("검정 오버사이즈 후드")도 → `...women`.

**패치 (3중)**:
1. **`_item_attrs` 영어 고정**: suggested_query 는 항상 영어 `searchQuery` 사용 (번역 단계 제거 → 성별 손실 차단).
2. **프롬프트 재설계**: "always include gender" → "신호 있을 때만 men/women, 없으면 OMIT (시스템이 unisex 부착)". women-leaning 편향 문구 제거.
3. **결정론적 unisex 핀**: `search_products._ensure_gender_token` — text_query 에 gender 토큰 없으면 `unisex` 부착. Haiku 의 women-편향과 무관하게 보장.

**검증**: S2 재실행 — LLM 출력 `text_query='black oversized hoodie'` (성별 생략 ✅) → 서버 실제 임베딩 `'black oversized hoodie unisex'` (헬퍼 부착 ✅). 명시 성별(`grey t-shirt men`)은 보존.

### §8 추가 패치 파일 (4)

| 파일 | 변경 |
|---|---|
| `app/channels/telegram/adapter.py` | send_media_group 실패-아이템 DROP 재시도 + `_post(return_error=)` |
| `app/agents/react_loop.py` | `_item_attrs` 영어 suggested_query + 프롬프트 gender 정책 재설계 |
| `app/agents/tool_registry.py` | search_products canonical-form gender 규칙 (omit→system unisex) |
| `app/agents/tools/search_products.py` | `_ensure_gender_token` 결정론적 unisex 핀 |
| `tests/test_agent_v2/test_search_text_only.py` | gender 핀 반영해 기대값 갱신 (2 테스트) |

### §8 잔여 (실 chat_id 사람-손 검증 권장)

- 발견 #1 의 더보기-앨범 재시도는 mock 으로 재현 불가(Telegram CDN 의존) — 본인이 더보기 다시 눌러 **묶여서 오는지** 확인 필요.
- 발견 #3 의 이미지→picker→검색 성별 보존도 실 picker 탭 필요 (러너 미지원, V4-F).

---

## 9. 2차 실 테스트 발견 + SPEC-GENDER-PIN-001 (2026-05-22 PM)

§8 패치 후 2차 실 테스트. 카드 묶음(#1)은 **작동 확인**(`message #4 WEBPAGE_CURL_FAILED → drop → n=5→4 → ok`). 추가 발견 3건.

### 발견 #A — 중복 상품 (동일 상품 2개 노출)

스크린샷: `4. Rier t-shirt, fog ₩1,295,000` + `5. Rier t-shirt, fog ₩1,295,000` — product_id는 다른데 brand+name+price 동일(스크랩 중복 / 변형). `diversify`의 dedup이 product_id만 봐서 `drops_dup=0`으로 통과.
**패치**: `diversify_service` 에 콘텐츠 레벨 dedup `(brand, name_norm, price)` 추가. id-only 가드와 병행.

### 발견 #B — 이미지 흐름 지연 + 상세 타이밍 로그 요청

`search_products → 29714ms` (로그). 원인: Modal `/embed/text` cold start(~19s)가 agent per-tool 타임아웃에 걸려 retry → 또 cold Modal = 이중고.
**패치**: 단계별 ⏱ 타이밍 로그 추가 —
- `🔍 [text_search] done ... · ⏱ embed=Xms rpc=Yms divers=Zms total=Wms`
- `🗃️ [embed_cache] ... · ⏱ lookup=Xms modal=Yms`
실측 예: warm Modal `embed=4426ms(modal=4368ms) rpc=422ms divers=2ms`. (Modal warm-keep는 별도 인프라 — V4)

### 발견 #C — 🐛 성별이 안 픽스됨 → SPEC-GENDER-PIN-001 (기능)

§8의 unisex-default는 "픽스"가 아님. 사용자 요구 확정: **성별 무조건 픽스, 모르면 1회 물어 영구 저장, 직접 지시("여자 걸로")는 그 요청만 override**.

**구현 (기능, migration 포함)**:
1. **Migration 0008**: `ai.user_taste_profile.gender TEXT` (dev-app PG 적용 완료, head 0007→0008).
2. **TasteProfile.gender** + `taste_profile_pg` SELECT/INSERT/UPDATE/`_row_to_profile` 배선.
3. **성별 해석** (`search_products` dispatch): 우선순위 ① 메시지 명시 gender(LLM이 text_query에 넣음, per-request override) > ② `taste_profile.gender`(영구) > ③ 둘 다 없음.
   - ③ + 순수텍스트 → 검색 대신 **[남성][여성][상관없음] 카드** 발사 + 원쿼리 `pending_gender` 저장 → `awaiting_gender` 반환(턴 종료).
   - ③ + 이미지픽 → 차단 안 함, `unisex` 폴백.
4. **`clarify:gender:{men|women|unisex}` 콜백** (ingest 인라인): profile.gender 영구 저장 + pending 쿼리 pop → 그 성별로 즉시 재검색·하이브리드 전달 (agent 미경유, 결정론적). 라우터는 `__end__`.
5. **프롬프트**: "신호 없으면 gender OMIT(시스템이 처리)" + "awaiting_gender 받으면 카드 이미 발사됨 → 짧은 한 줄로만 응답".

**E2E 검증** (신규 chat 999999111):
```
1) "검정 후드 추천해줘" → gender=None, last_results=0 (검색 안 함, 카드 발사) ✅
   로그: search_products → awaiting_gender
2) clarify:gender:men 탭 → gender='men' 영구저장, last_results=15 ✅
   로그: embed text_query='black hoodie men' → final=15
```
명시 override(`grey t-shirt men`) 보존, genderless+adapter없음 → unisex 폴백도 검증.

### §9 패치 파일

| 파일 | 변경 |
|---|---|
| `migrations/versions/0008_add_taste_gender.py` | gender 컬럼 (NEW) |
| `app/infrastructure/memory/taste_profile.py` | `TasteProfile.gender` |
| `app/infrastructure/memory/taste_profile_pg.py` | gender read/write |
| `app/agents/pending_gender.py` | pending 검색 store (NEW) |
| `app/agents/tools/search_products.py` | 성별 해석 + 카드 발사 + 타이밍 로그 |
| `app/providers/embedding.py` | embed_text 타이밍 로그 |
| `app/services/diversify_service.py` | 콘텐츠 레벨 dedup |
| `app/graphs/nodes/ingest.py` | `_handle_gender_pick` (콜백 → pin + 재검색) |
| `app/graphs/fashion_bot.py` | `clarify:gender:*` → `__end__` 라우팅 |
| `app/agents/react_loop.py` | gender 정책 프롬프트 + awaiting_gender 응답 가이드 |
| `tests/test_agent_v2/test_agent_loop.py` · `test_search_text_only.py` · `tests/test_agents/test_pre_messages_tools.py` | gender 핀 반영 |

전체 테스트 881개 통과 (ruff clean).

### §9 잔여 (실 텔레그램 확인 권장)

- 신규 유저 텍스트 검색 → 성별 카드 1회 → 탭 → 결과까지 오는 흐름.
- 저장된 성별이 다음 검색에 자동 적용(재질문 X)되는지.
- "여자 걸로 보여줘" 직접 지시 시 그 요청만 여성으로 가는지.
- 멀티-아이템 이미지에서 성별 안 뒤집히는지(Vision 성별 보존).

---

## 10. 3차 실 테스트 — refine 쿼리 드리프트 버그 (2026-05-22 PM2)

성별 카드(#2)·묶음(#1) 실테스트 통과 확인. 새 버그 1건.

### 발견 #D — 🐛 refine "더 저렴하게"가 무관한 상품 반환

실테스트: 회색 레이스 원피스 검색 후 "더 저렴하게 20만원 이하로" → 가방/향수/키체인 반환. 로그:
```
refine_search dispatch args={"action":"cheaper","max_price":200000}
embed text_query='더 저렴하게 해줘 20만원 이하로'   ← 한국어 지시문을 임베딩!
```
**원인**: `refine_search` 가 `ctx['text_query']` 를 base 로 썼는데, `react_loop._build_ctx` 가 매 턴 `ctx['text_query']` 를 **원본 유저 메시지**로 시드한다(line 336). refine 턴엔 그게 "더 저렴하게 해줘 20만원 이하로"(상품 설명 아닌 가격 명령) → 그 문장과 의미적으로 가까운 랜덤 저가품 반환. 직전 상품 쿼리('grey floral lace dress women')는 이번 턴 ctx 에 없음(per-turn 리셋).

**패치**:
1. **`app/agents/last_query.py`** (NEW) — chat_id별 마지막 성공 검색 쿼리 in-process store (pending_question/pending_gender 패턴).
2. `search_products` / `refine_search` / ingest gender-resume 가 성공 시 최종 (영어, 성별 핀된) 쿼리를 `set_last_query`.
3. `refine_search` 가 base_query 를 `get_last_query(chat_id)` 우선 사용 (없으면 ctx fallback) → 원본 상품 쿼리에 가격/필터 델타만 적용.
4. **프롬프트**: "동일 검색 미세조정(더 저렴하게/브랜드 빼고/다른 색)은 `refine_search` 사용 — search_products 로 재생성 금지(가격 드롭·드리프트)". LLM 이 가격조정에 refine_search 를 일관되게 고르도록.

**E2E 검증** (chat 999999333):
```
회색 레이스 원피스 → women 픽스 → "더 저렴하게 20만원 이하로"
→ refine_search → embed text_query='grey lace floral dress women' (직전 원피스 쿼리 재사용 ✅)
→ 💰 price_filter kept=5 dropped=10 max=200000 (가격 적용 ✅) → 원피스 5개 under 200k ✅
```
가방·향수 사라지고 가격도 정확. 프롬프트 수정 전엔 LLM이 search_products 로 재생성해 가격 누락 → 수정 후 refine_search 일관 선택.

### §10 패치 파일

| 파일 | 변경 |
|---|---|
| `app/agents/last_query.py` | 마지막 검색 쿼리 store (NEW) |
| `app/agents/tools/search_products.py` | 성공 쿼리 `set_last_query` |
| `app/agents/tools/refine_search.py` | base_query = last_query 우선 + 저장 |
| `app/graphs/nodes/ingest.py` | gender-resume 도 last_query 저장 |
| `app/agents/react_loop.py` | REFINE vs SEARCH 프롬프트 가이드 |
