---
id: SPEC-IMPLICIT-FB-001
version: 0.2.0
status: draft
created_at: 2026-05-11
updated: 2026-05-11
author: hchsa77@gmail.com
priority: P0
issue_number: null
labels: [feedback, agentic, postgres, langfuse, taste-profile, telegram]
---

# SPEC-IMPLICIT-FB-001: Implicit Feedback Capture (Card Impressions, Clicks, Re-Query) for Telegram Fashion Bot

## HISTORY

- 2026-05-11 (v0.1.0): 초안. `docs/research/conversational-shopping-agents.md`
  takeaway #4 + Section 3 ("every interaction is a label") 의 마지막 leg —
  암묵적 피드백 캡처 — 를 P0 agentic 베이스라인의 세 번째 축으로 추가. SPEC-MEMORY-001
  (Postgres + Alembic baseline + ai schema + `psycopg.AsyncConnectionPool`) 위에
  `ai.card_impression` 테이블을 alembic revision 0002 로 얹고, SPEC-OBSERVABILITY-002
  의 활성화된 Langfuse v3 trace tree 에 `implicit_feedback.{type}` span 을 한 줄 추가.
  명시적 신호(`crit:more` / `crit:less` / `clarify:*` / "ami 좋아해" 자유 텍스트)는
  SPEC-AGENTIC-CRITIQUE-001 + SPEC-AGENT-001 + SPEC-CLARIFY-CARDS-001 이 이미 완비 →
  여기서는 **암묵 신호 3종** (no-click, click, re-query) 만 다룬다. 새로운 검색 스코어링
  로직은 도입하지 않으며, 기존 `TasteProfile.reinforce_*` API 를 가중치만 다르게 호출.
- 2026-05-11 (v0.2.0): plan-auditor 1차 감사(0.91, PASS) MINOR 4건 정리.
  (D1) Non-Goals 중복 번호 `5.` 해소 — 18개 항목으로 재번호 부여하여 Exclusions
  리스트와 1:1 정합. 내부 cross-ref "Non-Goal #15" → "Non-Goal #16" 갱신. (D2)
  click ack 토스트 모순 해소 — DoD 와 OQ-7 모두 **silent ack**(empty
  `answerCallbackQuery`, 토스트 없음) 로 통일. 사유: Telegram 네이티브 기본 동작
  부합, 시각 노이즈 최소화, KO/EN 로컬라이즈 부담 제거. OQ-7 `[RESOLVED 2026-05-11]`
  마킹. REQ-FB-CLICK-001 본문 step 5 + DoD + e2e (b) 시나리오까지 cascade. (D3)
  callback_data 길이 예산 정합 — REQ-FB-UX-001 acceptance 의 "last 60 chars" →
  **"last 53 chars"** 로 정정 (Telegram 64B limit − `crit:click:` 11 chars prefix
  = 53 chars 가용). R6 와 동일 수치로 일치. 절단 시 suffix match 전략 명시. (D4)
  테스트 개수 표현 명확화 — "≥ 9 tests" → **"9 test files covering all 11 REQs
  (multiple test cases per file)"** 로 수정, 파일 수와 케이스 수 혼동 방지.

---

## Goal

현재 텔레그램 봇은 사용자의 **명시적** 피드백만 학습한다:

- `crit:more` / `crit:less` 콜백 (SPEC-AGENTIC-CRITIQUE-001 user-driven critique)
- `clarify:*` 콜백 (SPEC-CLARIFY-CARDS-001 결정형 카드)
- 자유 텍스트 "ami 좋아해" → `taste_update` 노드 (SPEC-AGENT-001 REQ-AGENT-005)

이 세 경로는 모두 **사용자가 의도적으로 신호를 보낸 경우만** 학습한다.
`docs/research/conversational-shopping-agents.md` takeaway #4 / Section 3 의 핵심 인사이트
— "every interaction is a label" — 는 정반대를 요구한다:

1. **카드가 사용자에게 노출됐는데 클릭이 한 번도 안 됐다면** → 그 카드의 brand / keywords 는
   사용자 취향과 약하게 어긋나 있다는 부드러운 부정 신호.
2. **카드 링크가 실제로 탭됐다면** → 그 카드의 brand / keywords 는 사용자 취향에
   강한 긍정 신호.
3. **사용자가 직전 결과 carousel 을 받은 직후, 짧은 시간 안에 새 이미지·텍스트로
   재질문하면** → 직전 결과 셋이 만족스럽지 않았다는 부드러운 부정 신호.

현재는 위 세 신호가 모두 버려진다. 본 SPEC 은 이 세 신호를 캡처하고
`TasteProfile.reinforce_*` 메서드를 통해 — **검색 파이프라인 스코어링을 건드리지 않고** —
기존 모델에 다른 가중치로 흘려보낸다.

핵심 설계 원칙:

1. **Lazy attribution (NOT background worker)**. 카드 노출은 모든 `send_results` emit
   시 `ai.card_impression` 에 INSERT. 노출의 attribution (클릭 안 됨 → 부드러운 부정) 은
   **다음 사용자 턴이 시작될 때** `ingest` 노드 안에서 만료된 impression 을 스캔해서
   처리한다. 별도의 worker, 백그라운드 timer, sweeping coroutine 없음.

   *이유*: (a) POC 단계에서 추가 백그라운드 process 운영비용 회피, (b) 사용자가 다시
   안 오면 그 사용자의 impression 도 안 만져도 되는 자연스러운 lazy semantics,
   (c) 단일 워커 `--workers 1` 가정 (SPEC-MEMORY-001 Concurrency 모델과 동일) 에서
   동시성 복잡도 0.

2. **Postgres only — 기존 풀 재사용**. SPEC-MEMORY-001 이 띄운 `psycopg.AsyncConnectionPool`
   을 그대로 사용. 새 풀, 새 connection, 새 lifespan hook 없음.

3. **TasteProfile 인터페이스 무변경**. `reinforce_liked_brand` /
   `reinforce_disliked_brand` / `reinforce_liked_keywords` / `reinforce_disliked_keywords`
   는 이미 `weight: float = 1.0` 파라미터를 받는다. 본 SPEC 은 호출 site 추가만 하고
   weight 만 다르게 넘긴다 (1.0 / 0.5 / 0.2). 검색 파이프라인 (`app/pipeline/search.py`,
   `app/pipeline/diversify.py`) 은 이미 `TasteProfile.boost_brands` / `exclude_brands` /
   `boost_keywords` 를 통해 영향을 받으므로 → 자동으로 다음 검색에 반영된다.

4. **Telegram URL 버튼은 콜백 안 옴** — `LinkPreviewButton(url=...)` 탭은 webhook 으로
   안 들어온다. 클릭 캡처는 **추가 inline keyboard 버튼 "👀 자세히"** 를 카드마다 하나
   더 박아서 `crit:click:{product_id}` 콜백을 생성해야 한다. UX 오버헤드는 인지하지만
   유일한 path. 기존 3개 critique 버튼 (`crit:more` / `crit:less` / `crit:cheap`)
   행 옆에 4번째 버튼으로 추가.

5. **Graceful no-op on degraded memory**. SPEC-MEMORY-001 fallback path 가 in-memory
   store 로 운영 중인 경우 (`memory_backend=in_memory`), 본 SPEC 의 모든 DB 쓰기는
   silently skip — 봇은 안 죽고, 추가 `ERROR` 로그도 안 남긴다 (DEBUG 한 줄). 명시적
   피드백은 in-memory store 와 메모리 같이 살아 있으므로 사용자 경험은 유지.

6. **Observable via Langfuse**. SPEC-OBSERVABILITY-002 활성화 이후 모든 implicit
   reinforcement event 는 `implicit_feedback.{no_click,click,re_query}` span 으로
   trace tree 에 자동 등장. 별도 dashboard 없음.

7. **외부 행위 byte-identical (except for one new button)**. 추가되는 사용자 가시
   변화는 카드 inline keyboard 에 "👀 자세히" 버튼 1개 추가뿐. 검색 결과 ranking,
   추천 logic, KO/EN sticky 언어, clarify 카드 흐름, respond 자연어는 모두 그대로.

이 SPEC 은 **WHAT** 과 **WHY** 만 정의한다. `ingest` 노드의 정확한 attribution scan
SQL 모양, "👀 자세히" 버튼 라벨의 KO/EN 분기 카피, fast-path 가중치 튜닝 (0.2 / 0.5 /
1.0 default 가 데이터를 보고 너무 노이지하다면 어떻게 조정할지) 등 **HOW** 는
`plan.md` 와 Run phase 에서 결정한다.

---

## Background

### 현재 학습 경로 vs 잃어버리는 신호

| 신호 종류 | 캡처 여부 | 처리 경로 |
|---|---|---|
| `crit:more:{idx}` 콜백 (사용자 명시) | ✓ | `critique_apply` → 사용자 명시 비평 → 다음 search 의 `critique_pending_delta` |
| `crit:less:{idx}` 콜백 (사용자 명시) | ✓ | `critique_apply` → 동일 (+ critique state 리셋) |
| `clarify:{axis}:{value}` 콜백 (사용자 명시) | ✓ | `apply_clarify` → `Session.boost_keywords` 누적 |
| "ami 좋아해" 자유 텍스트 (사용자 명시) | ✓ | `taste_update` → `TasteProfile.reinforce_liked_brand(weight=2.0)` |
| **카드 5개 노출 → 클릭 0회** (암묵) | ✗ | (현재 버려짐) |
| **카드 링크 탭** (암묵) | ✗ | (Telegram URL 버튼은 콜백 안 옴 — 캡처 불가능) |
| **carousel 직후 새 이미지/텍스트** (암묵) | ✗ | (현재 버려짐) |

세 번째 컬럼의 비대칭이 본 SPEC 이 메우는 갭이다.

### 왜 implicit 신호인가 (research 인용)

`docs/research/conversational-shopping-agents.md` Section 3 "Implicit feedback capture":

> "Every interaction is a label. A user who dismisses a recommendation without
> engagement has communicated something — albeit weakly. A user who clicks
> through has communicated something — strongly. An agentic shopping system
> that ignores these signals is leaving 80% of behavioral signal on the floor."

Takeaway #4 에서는:

> "The third leg of the agentic baseline — after persistent memory and trace
> visibility — is implicit signal capture. Without it, the agent learns only
> from the small fraction of users who bother to give explicit feedback."

SPEC-MEMORY-001 이 "first leg" (영속 메모리), SPEC-OBSERVABILITY-002 가 "second leg"
(trace visibility), 본 SPEC 이 "third leg" (암묵 신호 캡처) 를 완성한다.

### 왜 Lazy attribution 이 옳은가

대안:

| 옵션 | 장점 | 단점 | 결정 |
|---|---|---|---|
| (A) **백그라운드 worker** — 별도 asyncio task 가 60초마다 attribution scan + 갱신 | 사용자 활동에 무관하게 동작 | 추가 worker 운영, 단일 워커 가정 깨짐, 큰 cron-like 복잡도 | **기각** |
| (B) **다음 사용자 턴에서 lazy attribution** — `ingest` 노드가 만료 impression 스캔 | 추가 worker 0, 사용자 안 오면 work 0, 단순성 | 활성 안 한 사용자의 impression 은 영원히 안 만져짐 | **채택** |
| (C) **send_results 직후 즉시 attribution scan** | 가장 단순 | "click 이 안 됐다" 의 판별 시점이 너무 이름 (사용자가 5초 후에 클릭할 수도 있음) | **기각 — semantic 무의미** |

(B) 는 (C) 의 too-early 문제를 attribution window 로 해결하고 (A) 의 worker 복잡도를
없앤다. 영원히 안 만져지는 row 는 30일 cleanup loop (이미 `user_session` 용으로 도는
SPEC-MEMORY-001 의 cleanup task) 가 attributed + 7d 시점에 삭제.

### Click 캡처 메커니즘 (Telegram constraint)

Telegram Bot API 의 InlineKeyboardButton 은 두 종류:

1. `url=...` — 사용자 탭하면 브라우저로 열림. **봇 webhook 으로 콜백 안 옴.** Telegram
   서버가 이 정보를 봇에게 안 알린다 (privacy by design).
2. `callback_data=...` — 사용자 탭하면 봇 webhook 으로 `callback_query` 전송.

현재 카드는 URL 버튼 1개 ("🛒 Shop on {brand}", `app/graphs/nodes/send_results.py`
`_candidate_to_card`) + critique 버튼 3개 (`crit:more` / `crit:less` / `crit:cheap`).

URL 버튼 탭은 캡처 불가능하므로, **click 시그널을 얻으려면 추가 callback 버튼이 필수**.
"👀 자세히" 버튼 1개 추가 → 사용자가 탭하면 `crit:click:{product_id}` 콜백 발생 →
`critique_apply` 가 이 콜백을 받아서 click event 로 처리. (별도의 신규 노드 없음 —
`crit:` prefix routing 재활용.)

UX 비용: 카드당 버튼 행이 critique 3개 + click 1개 = 총 4개. Telegram 모바일 UI 에서
한 줄 또는 두 줄에 들어감 (`InlineKeyboardMarkup` row wrapping). `plan.md` 는 정확한
row layout 을 결정.

대안 검토:
- 사용자가 봇 안으로 새 메시지 보낼 때를 "engagement" 으로 간주: 노이즈 큼, click 과
  분리 불가능.
- URL 버튼을 봇 자체의 redirect endpoint 로 바꿈 (`https://kiko.ai/r/{token}`): 인프라
  비용 발생 + Pinterest-style URL 트래킹과 동일한 패턴이지만 dev-app 에 새 redirect
  서비스 운영 필요. **POC 단계 비용 대비 이득 부족 — 기각.**

→ "👀 자세히" 버튼 채택.

### Re-query 감지 (Session-state 기반)

사용자가 carousel 을 받은 직후 (`Session.state == RESULTS_SENT`), `RE_QUERY_WINDOW_S`
(default 90s) 이내에 새 이미지·텍스트 메시지를 보내면 직전 결과 셋이 만족스럽지 않았다는
시그널.

판별 조건 (모두 AND):

1. `Session.state == RESULTS_SENT`
2. `Session.last_results` 가 비어있지 않음
3. (현재 시간) - `Session.last_active` < `RE_QUERY_WINDOW_S`
4. 인바운드 메시지가 `crit:*` / `clarify:*` 콜백 이 **아님** (그건 명시 신호 — 이미 처리됨)
5. 인바운드 메시지가 사진 또는 텍스트인 새 query

판별은 `ingest` 노드 안에서 (lazy attribution 이 도는 같은 노드 안에서) 수행하고,
조건이 맞으면 `Session.last_results` 의 모든 product_id 에 대해 brand+keywords 를
soft-negative reinforce (`weight=RE_QUERY_WEIGHT`, default 0.5). 그 후 정상 plain 흐름
(vision → search → ...) 진행.

이 가중치 0.5 는 명시적 `crit:less` (1.0) 와 no-click soft-negative (0.2) 사이의 중간
값으로 의도. 데이터를 보고 조정 가능 (env var).

### SPEC-MEMORY-001 / SPEC-OBSERVABILITY-002 와의 관계

- **의존**: SPEC-MEMORY-001 의 Alembic baseline (revision 0001) + `ai` schema +
  `psycopg.AsyncConnectionPool` 인프라가 prerequisite. Revision 0002 는 0001 의
  자식으로 등장.
- **의존**: SPEC-OBSERVABILITY-002 의 v3 `observe` 래퍼 + working callback handler.
  Active 가 아니어도 (no-op fallback) 본 SPEC 코드는 작동하지만, 활성화 시 implicit
  feedback span 이 자동으로 trace tree 에 합류.
- **무관**: SPEC-AGENTIC-CRITIQUE-001 의 `evaluator` 와 본 SPEC 의 implicit 신호는
  서로 다른 단계 — evaluator 는 검색 결과 *생성 직후* self-critique, implicit feedback
  은 결과 *전송 후* 사용자 행동 관찰. 둘 다 같은 `TasteProfile` 에 영향을 줄 수 있지만
  서로 다른 가중치 (`crit:less` 사용자 1.0 vs no-click 0.2) 와 서로 다른 시점 (즉시 vs
  지연) 이라 충돌하지 않음.

---

## Architecture Snapshot (informative)

Today (explicit feedback only):

```
user message arrives
  ↓
ingest → ... → search → ... → send_results → respond
                                  │
                                  └─ writes Session.last_results, .shown_product_ids
                                     state → RESULTS_SENT

(no impression log, no click capture, no re-query detection)

user taps crit:less:2
  ↓
ingest → critique_apply (RESET critique_*) → search → ... (explicit only)
```

After this SPEC (implicit signals added):

```
send_results
  │
  ├─ existing: write Session.last_results, .shown_product_ids
  └─ NEW: INSERT INTO ai.card_impression (chat_id, product_id, brand, keywords,
         shown_at=now(), click_status=NULL,
         attribution_window_s=IMPLICIT_FB_ATTRIBUTION_WINDOW_S)
       (one row per sent_candidate, max _MAX_CARDS=5 per turn)
       observability: span="implicit_feedback.impression_logged"

user taps "👀 자세히" on card 2  →  callback_data="crit:click:{product_id}"
  ↓
ingest → critique_apply
            │
            └─ NEW branch (callback_data startswith "crit:click:"):
                 lookup product (from Session.last_results),
                 UPDATE ai.card_impression SET click_status='clicked', click_at=now()
                       WHERE chat_id=? AND product_id=? AND click_status IS NULL
                 reinforce_liked_brand(brand, weight=CLICK_WEIGHT=1.0)
                 reinforce_liked_keywords(keywords, weight=CLICK_WEIGHT)
                 observability: span="implicit_feedback.click"
            ↓
       (no further graph routing — emit ack message via callback_query.answer)

user sends new photo 60s after carousel  →  re-query within window
  ↓
ingest
  ├─ NEW Step A — lazy attribution scan:
  │     SELECT * FROM ai.card_impression
  │       WHERE chat_id=? AND click_status IS NULL
  │         AND shown_at + (attribution_window_s * interval '1 second') < now()
  │     for each row: reinforce_disliked_brand(brand, weight=NOCLICK_WEIGHT=0.2)
  │                  reinforce_disliked_keywords(keywords, weight=NOCLICK_WEIGHT)
  │     UPDATE matched rows SET click_status='attributed_no_click'
  │     observability: one span="implicit_feedback.no_click" per row
  │
  ├─ NEW Step B — re-query detection:
  │     IF Session.state == RESULTS_SENT
  │        AND Session.last_results non-empty
  │        AND (now() - Session.last_active) < RE_QUERY_WINDOW_S
  │        AND inbound is fresh image/text (not crit:* / clarify:*):
  │        for each product in Session.last_results:
  │            reinforce_disliked_brand(brand, weight=REQUERY_WEIGHT=0.5)
  │            reinforce_disliked_keywords(keywords, weight=REQUERY_WEIGHT)
  │        observability: one span="implicit_feedback.re_query"
  │
  └─ existing ingest behavior continues unchanged (lang sticky, state machine)

Background cleanup (extends SPEC-MEMORY-001's cleanup loop):
  Every SESSION_CLEANUP_INTERVAL_S:
    DELETE FROM ai.card_impression
      WHERE click_status IN ('clicked', 'attributed_no_click')
        AND COALESCE(click_at, shown_at + (attribution_window_s * interval '1 second'))
            < now() - interval '7 days'
```

**Affected modules in kikoai/ai (this SPEC)**:

- `app/channels/implicit_feedback.py` — NEW. 모듈 본체. 다음 함수 export:
  - `log_impressions(chat_id, products: list[Candidate]) -> None` (send_results 호출)
  - `record_click(chat_id, product_id: str, brand: str, keywords: list[str]) -> None`
  - `attribute_expired_impressions(chat_id) -> int` (ingest lazy step A)
  - `detect_and_apply_re_query(session, inbound_kind) -> bool` (ingest step B)
  - 위 4개 모두 `@observe(name="implicit_feedback.{type}", as_type="span")` 데코.
  - 단일 모듈 안에 두는 이유: 응집도 — 4개 함수가 공유 helper (`_taste_for`,
    `_keywords_for_product`, sanitized chat_id_hash) 를 쓰고, attribution logic
    이 한 곳에 모여 있어야 weight 튜닝과 PII 검증이 쉬움.
- `app/graphs/nodes/send_results.py` — MODIFIED. 두 가지 변경:
  - `_candidate_to_card` 의 critique 버튼 행에 `("👀 자세히" or "👀 View",
    f"crit:click:{c.id}")` 4번째 버튼 추가 (lang 분기).
  - `send_results` 마지막 (rows_affected > 0 이후) 에
    `await implicit_feedback.log_impressions(chat_id, sent_candidates)` 호출.
- `app/graphs/nodes/critique_apply.py` — MODIFIED. `crit:click:{product_id}` 분기
  추가: product 를 `Session.last_results` 에서 lookup → brand / keywords 추출 →
  `implicit_feedback.record_click(...)` 호출. **이 분기는 `critique_*` state 를 리셋
  하지 않는다** (click 은 명시적 critique 가 아니므로 retry budget 재시작 불필요).
- `app/graphs/nodes/ingest.py` — MODIFIED. 노드 본문 앞쪽에 두 step 추가:
  - Step A: `await implicit_feedback.attribute_expired_impressions(chat_id)`
  - Step B: `await implicit_feedback.detect_and_apply_re_query(session, inbound_kind)`
  - 두 step 모두 fire-and-forget 의미: 예외는 흡수하고 log + continue.
- `app/channels/session.py` — UNCHANGED. (Session.last_active 는 이미 존재; 추가
  필드 없음.)
- `app/channels/taste_profile.py` — UNCHANGED. (`reinforce_*` 메서드의 `weight`
  파라미터 시그니처는 이미 존재.)
- `app/providers/db_pool.py` — UNCHANGED. SPEC-MEMORY-001 의 풀 그대로 재사용
  (`get_pool()`).
- `app/core/config.py` — MODIFIED. 4개 새 env vars
  (`IMPLICIT_FB_ATTRIBUTION_WINDOW_S`, `IMPLICIT_FB_NOCLICK_WEIGHT`,
  `IMPLICIT_FB_CLICK_WEIGHT`, `IMPLICIT_FB_REQUERY_WEIGHT`) +
  `RE_QUERY_WINDOW_S` 1개.
- `migrations/versions/0002_create_card_impression.py` — NEW. Alembic revision
  (`down_revision = "0001_create_memory_tables"`).
- `app/channels/session_pg.py` — MODIFIED (minor). `PostgresSessionStore` 의 cleanup
  task SQL 에 `card_impression` 의 stale row 삭제 한 줄 추가 (REQ-FB-CLEANUP-001).
  대안적으로 별도 cleanup helper 함수를 `implicit_feedback.py` 에 두고 같은 task
  가 호출 — `plan.md` 가 정확한 위치 결정.
- `tests/test_implicit_feedback/test_impression_log.py` — NEW.
- `tests/test_implicit_feedback/test_click.py` — NEW.
- `tests/test_implicit_feedback/test_no_click_attribution.py` — NEW.
- `tests/test_implicit_feedback/test_re_query.py` — NEW.
- `tests/test_implicit_feedback/test_cleanup.py` — NEW.
- `tests/test_implicit_feedback/test_fallback.py` — NEW. in-memory backend 일 때
  silent skip.
- `tests/test_implicit_feedback/test_cost.py` — NEW. < 50ms overhead 검증.
- `tests/test_implicit_feedback/test_observability.py` — NEW. span 발행 검증.
- `tests/test_implicit_feedback/test_config.py` — NEW. 5개 env var 시그니처 검증.

**Reused, untouched modules**:

- `app/pipeline/**` — 검색 파이프라인 변경 없음. `TasteProfile` 가중치 변동이
  자동으로 다음 검색에 영향.
- `app/graphs/state.py` — `WorkingState` / `OutputState` 무변경.
- `app/graphs/routing.py` — 라우팅 edge 무변경.
- `app/graphs/fashion_bot.py` — 토폴로지 무변경.
- `app/graphs/nodes/{vision,pick_item,ask_clarify,apply_clarify,search,evaluator,
  taste_update,respond,resolve_image}.py` — 무변경.
- `app/channels/{factory,adapter,vision,vision_prompt,clarify,clarify_values,lang,
  link_resolver}.py` — 무변경.
- `app/channels/telegram/*` — 무변경. inline keyboard 변경은 `send_results` 의
  `_candidate_to_card` 안에서만 — 어댑터 인터페이스 외부에는 보이지 않음.
- `app/api/{health,recommend,webhooks/telegram}.py` — 무변경.
- `app/observability/langfuse.py` — 무변경. `@observe` 사용만.

---

## Schema Reference (informative — formalized in REQ-FB-MIGRATION-001)

### `ai.card_impression`

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial PRIMARY KEY` | Synthetic PK. 단일 row 식별 + 인덱스 효율. |
| `chat_id` | `bigint NOT NULL` | Telegram chat id. `ai.user_session.chat_id` 와 같은 의미. FK 는 안 검 (R3). |
| `from_user_id` | `bigint` | nullable. taste_profile lookup 용. |
| `product_id` | `text NOT NULL` | Candidate.id. |
| `brand` | `text NOT NULL DEFAULT ''` | Candidate.brand (lowercased application-side). |
| `keywords` | `jsonb NOT NULL DEFAULT '[]'::jsonb` | `list[str]` (lowercased). reinforce_*_keywords payload. |
| `shown_at` | `timestamptz NOT NULL DEFAULT now()` | impression emit 시점. |
| `attribution_window_s` | `integer NOT NULL` | 이 row 의 attribution window. `IMPLICIT_FB_ATTRIBUTION_WINDOW_S` 의 snapshot — 런타임에 env 가 바뀌어도 이 row 의 attribution 시점은 변하지 않음. |
| `click_status` | `text` | nullable. `NULL` (pending) / `'clicked'` (사용자가 탭) / `'attributed_no_click'` (만료된 채 click 안 됨 → no-click 가중치 적용 완료). |
| `click_at` | `timestamptz` | nullable. `click_status='clicked'` 시 set. |

Indexes:

- `PRIMARY KEY (id)`.
- `INDEX idx_card_impression_chat_pending (chat_id) WHERE click_status IS NULL` —
  partial index. `attribute_expired_impressions` 의 hot query
  (`WHERE chat_id=? AND click_status IS NULL`) 의 표적.
- `INDEX idx_card_impression_chat_product (chat_id, product_id)` — click event 시
  product lookup.
- `INDEX idx_card_impression_cleanup (click_at, shown_at) WHERE click_status IS NOT NULL` —
  cleanup loop 의 7d expiry scan.

Foreign keys: **없음** (R3 — `user_session` row 가 cleanup 으로 사라져도 impression 은
독립적으로 남아 self-attribution 가능; cleanup 정책은 application 이 관리).

JSONB 폴리시: `keywords` 는 `list[str]` 로 저장; psycopg3 JSONB 자동 변환에 의존
(SPEC-MEMORY-001 의 패턴과 동일).

### Status 전이

```
NULL (pending)
  ├─► 'clicked'              (사용자 "👀 자세히" 탭 / record_click)
  └─► 'attributed_no_click'  (만료 후 ingest lazy scan / attribute_expired_impressions)
```

전이는 단방향. `'clicked'` 와 `'attributed_no_click'` 사이의 race condition (사용자가
attribution window 끝나기 1ms 전에 탭) 은 무시 — `click_status IS NULL` predicate
의 atomic UPDATE 가 결정. 이미 attributed 된 row 에 대한 늦은 click 은 silently
skip (`UPDATE ... WHERE click_status IS NULL` 의 `0 rows affected` 정상 처리).

---

## Requirements & Acceptance Criteria

### REQ Index

| REQ-ID | Title | Priority |
|---|---|---|
| REQ-FB-IMPRESSION-001 | `send_results` SHALL log every sent card to `ai.card_impression` | P0 |
| REQ-FB-CLICK-001 | `crit:click:{product_id}` callback reinforces liked_* with click weight | P0 |
| REQ-FB-NOCLICK-001 | Expired impressions credited on next user turn (lazy) | P0 |
| REQ-FB-REQUERY-001 | Rapid re-query soft-negative reinforces last shown set | P0 |
| REQ-FB-MIGRATION-001 | Alembic revision 0002 creates `ai.card_impression` with indexes | P0 |
| REQ-FB-CLEANUP-001 | Attributed-or-clicked rows older than 7d are hard-deleted | P0 |
| REQ-FB-OBSERVABILITY-001 | Each implicit reinforcement event produces a Langfuse span | P0 |
| REQ-FB-COST-001 | Implicit feedback adds < 50ms p99 overhead per webhook | P0 |
| REQ-FB-FALLBACK-001 | Postgres unreachable / degraded → silent skip | P0 |
| REQ-FB-CONFIG-001 | 5 env vars wired through `app/core/config.py` | P0 |
| REQ-FB-UX-001 | "👀 자세히" button added to every card with KO/EN copy | P0 |

---

### Impression Logging (REQ-FB-IMPRESSION-*)

#### REQ-FB-IMPRESSION-001 — `send_results` SHALL persist one impression row per successfully sent card [P0]

**WHEN** `app/graphs/nodes/send_results.py::send_results` finishes its inner loop
with at least one `sent_candidate`,
**THE SYSTEM SHALL** insert one row into `ai.card_impression` per sent candidate,
populating `chat_id`, `from_user_id` (from `Session.from_user_id`), `product_id`
(from `Candidate.id`), `brand` (lowercased), `keywords` (the same set the search
pipeline computed for that product — derived from `Candidate.subcategory`,
`Candidate.name` tokens, or whatever the existing keywords source is — exact
extraction defined in `plan.md`), `shown_at=now()`,
`attribution_window_s=settings.IMPLICIT_FB_ATTRIBUTION_WINDOW_S` (snapshot),
`click_status=NULL`, `click_at=NULL`.

**Acceptance**:

- An integration test exercises `send_results` with a 3-card sent batch and
  asserts exactly 3 rows are inserted into `ai.card_impression`, all with
  `click_status IS NULL` and `shown_at` within ± 1s of the test wall clock.
- The insertion SHALL be a single batched statement (`INSERT INTO
  ai.card_impression (...) VALUES (...), (...), (...)`) or `executemany` —
  **NOT** N separate round-trips. Goal: < 5ms added latency per `send_results`
  call. `plan.md` confirms the exact psycopg pattern.
- If the candidate has no `id` field (defensive — should not happen in practice),
  the row SHALL be skipped silently with a DEBUG log line `[IMPLICIT_FB][skip]
  missing product_id` — the impression is lost but the rest of the batch
  proceeds.
- Insertion failure (database error, pool exhausted) SHALL be caught and logged
  at WARN level; `send_results` MUST still complete normally and the user MUST
  still receive the cards. The lost impression is acceptable; the bot must
  never fail to deliver a recommendation due to an impression-log error.
- The impression insertion SHALL execute AFTER `get_store().update(sess)` (the
  existing line that persists `Session.last_results`) so the impression row
  matches what the session believes was shown.
- A unit test asserts the `keywords` column receives lowercased strings only
  (no mixed case, no empty strings) — `_keywords_for_product` is responsible.

---

### Click Capture (REQ-FB-CLICK-*)

#### REQ-FB-CLICK-001 — `crit:click:{product_id}` callback reinforces `liked_*` with click weight [P0]

**WHEN** the inbound message is a Telegram callback_query with `callback_data`
matching the pattern `crit:click:{product_id}` (where `product_id` is the
exact `Candidate.id` of one of the cards in `Session.last_results`),
**THE SYSTEM SHALL** route it through `critique_apply` to a NEW branch that:

1. Look up the product in `Session.last_results` by exact id match. If not
   found (stale callback — user tapped a card from a previous round whose
   session was cleared), emit a DEBUG log `[IMPLICIT_FB][stale-click]
   product_id=...` and silently ack the callback. NO reinforcement applied,
   NO impression UPDATE.
2. If found, atomically `UPDATE ai.card_impression SET click_status='clicked',
   click_at=now() WHERE chat_id=$1 AND product_id=$2 AND click_status IS NULL`.
   The `WHERE click_status IS NULL` predicate prevents double-credit if the
   user taps the same button twice rapidly.
3. Regardless of the UPDATE's rows-affected count, call
   `TasteProfile.reinforce_liked_brand(brand,
   weight=IMPLICIT_FB_CLICK_WEIGHT)` AND
   `TasteProfile.reinforce_liked_keywords(keywords,
   weight=IMPLICIT_FB_CLICK_WEIGHT)`. Rationale: even if the DB UPDATE was a
   no-op (already attributed or already clicked), the click intent is real and
   the taste profile reinforcement is desired exactly once per UI tap — the
   DB row's role is bookkeeping, the TasteProfile's role is learning. The "exactly
   once" property comes from the user's UI behavior, not from the DB row state.
   (A misbehaving double-tap by the user is treated as two separate signals —
   reinforcement is naturally idempotent under the `_DECAY=0.9` semantics.)
4. NOT reset `critique_*` state fields. Click is an implicit positive signal,
   not an explicit retry request — Reflexion retry budget should not restart.
5. Acknowledge the callback (`callback_query.answer`) **silently** with
   empty text — no toast, no banner. Telegram's default callback-ack behavior
   (button "depresses" briefly) is the only visual signal. Rationale: minimal
   visual noise + no KO/EN localization burden (OQ-7 resolved). No
   `sendMessage` triggered.
6. Set `OutputState.sent_count=0` and exit the graph (no further routing —
   the click acknowledgment IS the response).

**Acceptance**:

- An integration test plays a callback `crit:click:abc123` for a product in
  `Session.last_results` and asserts:
  - `ai.card_impression` row for (chat_id, "abc123") transitions from
    `click_status=NULL` to `click_status='clicked'` with `click_at` set.
  - `TasteProfile.liked_brands[brand]` value increases by `IMPLICIT_FB_CLICK_WEIGHT`
    (modulo the existing `_DECAY=0.9` scaling — exact assertion via
    `math.isclose(rel_tol=1e-9)`).
  - `Session.critique_retry_count` is unchanged (NOT reset).
  - Bot does NOT emit a new chat message; only `answerCallbackQuery` is called
    (verified via adapter mock).
- An integration test plays the same callback for a `product_id` NOT in
  `Session.last_results` and asserts: no DB UPDATE, no TasteProfile change,
  `answerCallbackQuery` still called (silent ack).
- An integration test plays the same callback TWICE rapidly (same product) and
  asserts: first call transitions row to `clicked`, second call's UPDATE
  affects 0 rows (idempotent), and TasteProfile receives 2 reinforcement calls
  (the natural-decay semantics absorb the duplicate gracefully — this is
  documented behavior, not a bug).
- A unit test asserts `crit:click:` is a distinct branch from `crit:more:` /
  `crit:less:` / `crit:cheap:` in `critique_apply` — accidental fall-through
  to the existing critique reset path would corrupt retry state. The branch
  ordering / exclusivity is tested explicitly.

---

### No-Click Attribution (REQ-FB-NOCLICK-*)

#### REQ-FB-NOCLICK-001 — On every user turn, expired impressions for that chat_id SHALL be credited as soft-negative [P0]

**WHEN** the `ingest` node executes for a chat_id (every webhook entry),
**THE SYSTEM SHALL** issue ONE SQL statement
```
WITH expired AS (
  SELECT id, brand, keywords FROM ai.card_impression
   WHERE chat_id = $1
     AND click_status IS NULL
     AND shown_at + (attribution_window_s * interval '1 second') < now()
)
UPDATE ai.card_impression
   SET click_status = 'attributed_no_click'
 WHERE id IN (SELECT id FROM expired)
RETURNING brand, keywords;
```
(or an equivalent CTE — `plan.md` confirms exact SQL). For each row in the
RETURNING result, call:

- `TasteProfile.reinforce_disliked_brand(brand,
  weight=IMPLICIT_FB_NOCLICK_WEIGHT)`
- `TasteProfile.reinforce_disliked_keywords(keywords,
  weight=IMPLICIT_FB_NOCLICK_WEIGHT)`

After the loop, call `get_taste_store().update(profile)` once.

**Acceptance**:

- An integration test seeds 3 impression rows for `chat_id=X`: row A
  (shown_at=20min ago, attribution_window=600s — expired), row B (shown_at=2min
  ago, attribution_window=600s — pending), row C (shown_at=20min ago,
  attribution_window=600s, but `click_status='clicked'` — should not be
  re-attributed). Calls `attribute_expired_impressions(X)` and asserts:
  - Row A: transitions to `'attributed_no_click'`, contributes one disliked_brand
    + disliked_keywords reinforcement.
  - Row B: untouched.
  - Row C: untouched.
  - `TasteProfile.disliked_brands[A.brand]` and
    `disliked_keywords[k]` for each `k in A.keywords` increased by
    `IMPLICIT_FB_NOCLICK_WEIGHT` (under `_DECAY=0.9` rules).
- The scan SHALL execute as a SINGLE round-trip (CTE) — not "SELECT then UPDATE"
  in two trips. Validated by a connection-counter / log-capture test.
- The scan SHALL be bounded — for a given chat_id the expired-row count is at
  most `_MAX_CARDS=5` per past turn × number of past turns within 30d retention.
  In steady state this is ≤ ~150 rows. A query-plan inspection test (`EXPLAIN`)
  asserts the partial index `idx_card_impression_chat_pending` is used.
- If the SQL fails for any reason (DB unreachable, schema drift, etc.), the
  `ingest` node SHALL log at WARN level and continue with the rest of its
  normal logic. The webhook MUST complete normally.
- A unit test with `MEMORY_FALLBACK_ON_PROBE_FAIL=true` and unreachable
  Postgres asserts the function silently returns 0 (no error log at WARN,
  just DEBUG) so the bot is not noisy in degraded mode.

---

### Re-Query Detection (REQ-FB-REQUERY-*)

#### REQ-FB-REQUERY-001 — Rapid re-query SHALL soft-negatively reinforce last shown set [P0]

**WHEN** all of the following are true at `ingest` node entry:

1. `Session.state == SessionState.RESULTS_SENT`
2. `Session.last_results` is non-empty (list length ≥ 1)
3. `(time.time() - Session.last_active) < settings.RE_QUERY_WINDOW_S`
4. The inbound `ChannelMessage` is NOT a `crit:*` callback AND NOT a `clarify:*`
   callback (i.e., it is a fresh image, link, or free text)

**THE SYSTEM SHALL** apply soft-negative reinforcement to each product in
`Session.last_results`:

- For each `c in Session.last_results`:
  - `TasteProfile.reinforce_disliked_brand(c.brand,
    weight=IMPLICIT_FB_REQUERY_WEIGHT)`
  - `keywords = _keywords_for_product(c)` (same extractor as
    REQ-FB-IMPRESSION-001)
  - `TasteProfile.reinforce_disliked_keywords(keywords,
    weight=IMPLICIT_FB_REQUERY_WEIGHT)`
- `get_taste_store().update(profile)` once.
- Emit one INFO log `[IMPLICIT_FB][re-query] chat_id_hash=... products=N
  elapsed_since_results_s=...`.

This SHALL run AFTER REQ-FB-NOCLICK-001's expired-impression scan but BEFORE
the rest of the ingest logic (vision routing, lang detection, etc.). Reason:
both touch `TasteProfile`; running them in a deterministic order keeps the
trace clean.

**Acceptance**:

- An integration test sets up `Session.state=RESULTS_SENT`,
  `last_results=[CandidateA, CandidateB]`, `last_active = now() - 60s`,
  `RE_QUERY_WINDOW_S=90`. Sends a new photo message. Asserts:
  - Both candidates' brands contribute to `disliked_brands` with weight
    `IMPLICIT_FB_REQUERY_WEIGHT`.
  - Both candidates' keywords contribute to `disliked_keywords` similarly.
  - One `[IMPLICIT_FB][re-query]` log line emitted.
  - The rest of the ingest flow (vision routing) proceeds normally — the new
    photo IS processed.
- An integration test sets the same setup but `last_active = now() - 120s`
  (past the window) and asserts NO re-query reinforcement applied.
- An integration test with `Session.state=AWAITING_ITEM_PICK` (not
  `RESULTS_SENT`) and otherwise same conditions asserts NO re-query
  reinforcement.
- An integration test with an inbound `clarify:formality:casual` callback (a
  user-driven explicit signal) within the window asserts NO re-query
  reinforcement — the explicit signal trumps the implicit one and we don't
  double-count.
- The reinforcement SHALL run BEFORE the normal `Session.state` transition
  (RESULTS_SENT → IDLE or similar) — otherwise the `Session.last_results`
  reference would be wiped before we read it. Validated by ordering test.
- A unit test asserts the per-product `keywords` extractor is identical to the
  one used in REQ-FB-IMPRESSION-001 — both sites must agree, or no-click and
  re-query signals would target different keyword sets for the same product.

---

### Migration (REQ-FB-MIGRATION-*)

#### REQ-FB-MIGRATION-001 — Alembic revision 0002 SHALL create `ai.card_impression` with documented indexes [P0]

**THE SYSTEM SHALL** introduce a new Alembic revision file
`migrations/versions/0002_create_card_impression.py` with
`down_revision = "0001_create_memory_tables"`. The revision SHALL create the
table `ai.card_impression` with the schema in the Schema Reference section,
including all 4 indexes. The `downgrade()` function SHALL `DROP TABLE
ai.card_impression`.

**Acceptance**:

- `alembic upgrade head` on a clean dev Postgres (with revision 0001 already
  applied) creates the `ai.card_impression` table with all 10 columns, the
  correct types, defaults, and 4 indexes. Verified by `\d ai.card_impression`
  output snapshot.
- `alembic downgrade -1` cleanly drops the table without affecting
  `user_session` or `user_taste_profile`.
- The DDL SHALL use `CREATE TABLE IF NOT EXISTS` (raw `op.execute` if needed)
  and `CREATE INDEX IF NOT EXISTS` to be idempotent under re-run, matching the
  pattern from revision 0001.
- The `attribution_window_s` column SHALL be a plain `integer NOT NULL` — no
  default at the DB level. The application is responsible for snapshotting
  the env var into every INSERT (so historical rows preserve their original
  attribution intent even if env changes later).
- The migration SHALL run within ≤ 1 second on an empty database (no large
  data operations, just DDL).
- `alembic check` (if/when SPEC-MEMORY-001's CI hook is wired) SHALL not flag
  drift between this revision and the application's INSERT statements.

---

### Cleanup (REQ-FB-CLEANUP-*)

#### REQ-FB-CLEANUP-001 — Attributed-or-clicked rows older than 7 days SHALL be hard-deleted [P0]

**THE SYSTEM SHALL** extend the existing background cleanup task in
`app/channels/session_pg.py::PostgresSessionStore` (started in lifespan per
SPEC-MEMORY-001 REQ-MEMORY-SESSION-002) to ALSO run:

```
DELETE FROM ai.card_impression
 WHERE click_status IS NOT NULL
   AND COALESCE(click_at, shown_at + (attribution_window_s * interval '1 second'))
       < now() - interval '7 days';
```

Run frequency: same as the existing session cleanup
(`SESSION_CLEANUP_INTERVAL_S`, default 300s). The DELETE SHALL log one INFO
line `[IMPLICIT_FB][cleanup] deleted=N` per iteration.

Rows with `click_status IS NULL` (pending attribution) SHALL NOT be cleaned
by this task — they are the responsibility of the lazy attribution scan
(REQ-FB-NOCLICK-001). However, to bound unbounded growth from chat_ids that
never come back, a SECOND DELETE clause SHALL also run:

```
DELETE FROM ai.card_impression
 WHERE click_status IS NULL
   AND shown_at < now() - interval '60 days';
```

This handles the edge case where a user stops using the bot forever and their
pending impressions are never attributed by the lazy path.

**Acceptance**:

- An integration test seeds 5 rows: A (clicked, 8 days old), B (clicked, 3 days
  old), C (attributed_no_click, 8 days old), D (attributed_no_click, 6 days old),
  E (pending NULL, 70 days old). Runs the cleanup task once. Asserts: A, C, E
  deleted; B, D remain.
- The cleanup task SHALL emit one INFO log line per iteration with the deleted
  count (one per DELETE clause is acceptable).
- The cleanup task SHALL NOT block the session cleanup that runs in the same
  iteration — both DELETEs are independent statements on independent indexes.
- A unit test asserts the second DELETE (60d pending) is rare in practice but
  exists as a safety net — a test seeds 1 row aged 70 days with NULL status
  and asserts it is deleted on the next cleanup tick.
- The DELETE SHALL use the `idx_card_impression_cleanup` index (verified via
  EXPLAIN in a test).

---

### Observability (REQ-FB-OBSERVABILITY-*)

#### REQ-FB-OBSERVABILITY-001 — Each implicit reinforcement event SHALL produce a Langfuse span [P0]

**THE SYSTEM SHALL** decorate the four core functions in
`app/channels/implicit_feedback.py` with `@observe(name="implicit_feedback.
{type}", as_type="span")` where `type` is one of:

- `impression_logged` — `log_impressions(...)` (one span per call, batch of N
  inserts inside).
- `click` — `record_click(...)` (one span per click).
- `no_click` — `attribute_expired_impressions(...)` (one span per call; the
  attributed-count goes into span metadata, not one span per row).
- `re_query` — `detect_and_apply_re_query(...)` (one span per detected re-query;
  if condition not met, the function returns early and emits the span anyway
  with `metadata.triggered=False` so we can see the negative-case rate).

Each span SHALL carry the following metadata via
`langfuse.update_current_span(metadata={...})` (v3 client API per
SPEC-OBSERVABILITY-002):

| Span | Required metadata keys |
|---|---|
| `impression_logged` | `chat_id_hash`, `count` (rows inserted), `elapsed_ms`, `backend` ("postgres" / "in_memory_skipped") |
| `click` | `chat_id_hash`, `product_id` (the raw product_id is fine — not PII; product catalog id), `brand`, `weight`, `stale` (bool — `True` if product not in last_results), `db_rows_affected`, `elapsed_ms` |
| `no_click` | `chat_id_hash`, `attributed_count`, `attribution_age_s` (avg or max — `plan.md` decides), `weight`, `elapsed_ms` |
| `re_query` | `chat_id_hash`, `triggered` (bool), `products_count` (when triggered), `elapsed_since_results_s`, `weight`, `elapsed_ms` |

`chat_id_hash` SHALL use the `_hash_for_span` helper from SPEC-OBSERVABILITY-002
REQ-OBS-PII-001 (sha256-prefix-16). Raw `chat_id` and raw `from_user_id` MUST
NOT appear in any of these spans' metadata.

**Acceptance**:

- A unit test with a mock Langfuse client asserts each of the 4 functions
  emits exactly one span named per the table above. The metadata dict
  contains all required keys with the documented types.
- A PII test (same pattern as SPEC-OBSERVABILITY-002 REQ-OBS-PII-001 dynamic
  test) runs `log_impressions(999999999, [c1, c2])` and asserts the literal
  string `"999999999"` does NOT appear anywhere in the captured span payload.
- When SPEC-OBSERVABILITY-002 is in no-op fallback (`LANGFUSE_PUBLIC_KEY=""`),
  the decorations are transparent passthroughs — the four functions still
  produce their normal behavioral side effects. Asserted by a test that runs
  the full flow with keys cleared and verifies DB rows / TasteProfile changes
  unchanged.
- The `product_id` field on the `click` span is the catalog product id (text)
  and SHALL NOT be hashed — it is reference data, not PII, and is needed to
  cross-reference clicks with product catalog analytics.

---

### Cost (REQ-FB-COST-*)

#### REQ-FB-COST-001 — Implicit feedback SHALL add < 50ms p99 overhead per webhook [P0]

**THE SYSTEM SHALL** ensure the per-webhook latency overhead added by implicit
feedback machinery (lazy attribution scan + re-query check + impression INSERT
on outbound + observability spans) is bounded as below. Measurement baseline:
same post-SPEC build with `IMPLICIT_FB_ATTRIBUTION_WINDOW_S=0` AND
`RE_QUERY_WINDOW_S=0` (both conditions inert — the code paths still execute
but neither attribution nor re-query reinforcement fires).

| Percentile | Threshold |
|---|---|
| p50 | < 10ms |
| p95 | < 30ms |
| p99 | < 50ms |

Measured end-to-end across the webhook handler entry to the channel adapter's
`sendMessage` call.

**Acceptance**:

- A benchmark test in `tests/test_implicit_feedback/test_cost.py` runs 500
  iterations of a synthetic webhook (mocked Telegram update, real testcontainers
  Postgres, mocked Vision LLM, mocked search RPC). Configurations:
  1. **Baseline**: `IMPLICIT_FB_ATTRIBUTION_WINDOW_S=0` AND `RE_QUERY_WINDOW_S=0`.
     Lazy scan finds 0 expired rows (windows are 0 → nothing is expired);
     re-query never triggers. Impression INSERTs still happen on outbound.
  2. **Measurement**: defaults (`ATTRIBUTION_WINDOW_S=600`,
     `RE_QUERY_WINDOW_S=90`). Seed 10 expired impressions for the test chat_id
     before each iteration so the lazy scan does real work.
  Asserts the p50 / p95 / p99 delta is below the thresholds above.
- The benchmark SHALL use `time.perf_counter_ns()` and run against a clean
  testcontainers Postgres (warm pool). Flakiness budget: 20% headroom (effective
  p99 threshold 60ms in CI).
- The impression INSERT SHALL be a single batched statement (REQ-FB-IMPRESSION-001
  acceptance), capped at 5 rows per call. Expected INSERT latency ≤ 5ms p95.
- The lazy attribution scan SHALL be a single CTE round-trip (REQ-FB-NOCLICK-001
  acceptance). Expected scan latency ≤ 10ms p95 for chat_ids with ≤ 50 expired
  rows.
- The re-query check itself is in-memory (Session field reads + timestamp
  comparison) — < 1ms overhead.
- If REQ-FB-COST-001 fails in CI, the rollback is: invoke REQ-FB-FALLBACK-001's
  emergency env var `IMPLICIT_FB_ENABLED=false` (deferred: see Open Questions)
  — but this is a knob, not a per-SPEC commitment. For now the threshold
  enforcement IS the gate.

---

### Fallback (REQ-FB-FALLBACK-*)

#### REQ-FB-FALLBACK-001 — Postgres unreachable / in-memory backend SHALL silently skip [P0]

**WHEN** the active memory backend is `in_memory` (per SPEC-MEMORY-001
REQ-MEMORY-HEALTH-001 — the bot is in degraded fallback mode),
**THE SYSTEM SHALL** silently skip all four `implicit_feedback.*` operations
(no DB writes, no scan, no re-query check). Each function returns its no-op
result (e.g., `log_impressions` returns 0, `attribute_expired_impressions`
returns 0). The Langfuse span SHALL still be emitted with
`metadata.backend="in_memory_skipped"` so the skip is observable, but the
DEBUG-level log only — NO WARN/ERROR per webhook (would flood logs in
degraded mode).

**WHEN** the memory backend is `postgres` BUT a specific operation fails (DB
unreachable mid-session, query timeout, schema mismatch),
**THE SYSTEM SHALL** catch the exception, log ONE WARN line with the
operation name + truncated exception, and continue. The webhook MUST complete
normally. The lost signal is acceptable; the bot must never fail to deliver a
recommendation, ack a click, or process an inbound message due to an implicit
feedback error.

**Acceptance**:

- A unit test sets the registered store to `InMemorySessionStore` (mimicking
  degraded mode), calls each of the 4 functions, and asserts:
  - No exception raised.
  - Return values are the documented no-op values.
  - No DEBUG-level log line at level WARN or above is emitted.
  - Langfuse span (if active) carries `backend="in_memory_skipped"`.
- A unit test patches the psycopg pool's `connection()` to raise
  `psycopg.OperationalError` on use, and asserts each function returns the
  no-op value AND emits exactly one WARN log line.
- The detection of "memory backend = postgres vs in_memory" SHALL be O(1) — a
  module-level cached flag set during lifespan startup, NOT a per-call
  `isinstance()` check on `get_store()`. `plan.md` confirms the wiring (likely
  a `MEMORY_BACKEND_IS_POSTGRES` boolean read from a shared module).
- The bot's user-visible behavior in degraded mode SHALL be byte-identical to
  the bot before SPEC-IMPLICIT-FB-001 landed — no extra messages, no missing
  cards, no slower webhooks (in-memory skip is faster than DB roundtrip).
- The bot MUST NOT attempt to recover the postgres backend mid-session;
  fallback is one-way until restart (matches SPEC-MEMORY-001 Non-Goal #8).

---

### Configuration (REQ-FB-CONFIG-*)

#### REQ-FB-CONFIG-001 — Five env vars SHALL be declared in `app/core/config.py` and documented in `.env.example` [P0]

**THE SYSTEM SHALL** introduce the following typed settings in
`app/core/config.py::Settings`:

| Setting | Type | Default | Notes |
|---|---|---|---|
| `IMPLICIT_FB_ATTRIBUTION_WINDOW_S` | `int` | `600` | Seconds after `shown_at` before no-click attribution kicks in. Snapshotted per-row at INSERT time. |
| `IMPLICIT_FB_NOCLICK_WEIGHT` | `float` | `0.2` | `reinforce_disliked_*` weight on no-click attribution. |
| `IMPLICIT_FB_CLICK_WEIGHT` | `float` | `1.0` | `reinforce_liked_*` weight on click. |
| `IMPLICIT_FB_REQUERY_WEIGHT` | `float` | `0.5` | `reinforce_disliked_*` weight on rapid re-query. |
| `RE_QUERY_WINDOW_S` | `int` | `90` | Seconds after `Session.last_active` (in RESULTS_SENT state) within which a new query is treated as re-query. |

Each var SHALL be loaded once at startup (no hot reload) and exposed as a
typed property on `settings`. `.env.example` SHALL document each with a one-line
comment explaining intent + the weight relationship (click 1.0 > re-query 0.5
> no-click 0.2 > decay 0.9).

**Acceptance**:

- A unit test imports `app.core.config.settings` and asserts all 5 attributes
  exist with the documented default values.
- A unit test asserts `IMPLICIT_FB_ATTRIBUTION_WINDOW_S > 0` and
  `RE_QUERY_WINDOW_S > 0` at startup (range guard); 0 or negative values are
  clamped to the default with a startup WARN log. Rationale: the cost-benchmark
  baseline (REQ-FB-COST-001) uses `=0` as a test-only configuration; production
  must not run with these effectively-disabled. (Note: the test for
  REQ-FB-COST-001 explicitly bypasses this clamp via direct setting override
  in the test fixture, not via env var.)
- `.env.example` contains all 5 vars with comments.
- The weight invariant
  `IMPLICIT_FB_CLICK_WEIGHT > IMPLICIT_FB_REQUERY_WEIGHT > IMPLICIT_FB_NOCLICK_WEIGHT`
  SHALL be documented as an informative convention in the .env.example but NOT
  enforced at startup (operator may legitimately tune weights below decay floor
  for experimentation).

---

### UX (REQ-FB-UX-*)

#### REQ-FB-UX-001 — Every card SHALL include a "👀 자세히" / "👀 View" callback button [P0]

**WHEN** `_candidate_to_card` builds the `BotCard` for a candidate,
**THE SYSTEM SHALL** add a 4th critique button to `critique_buttons` with:

- Korean (lang=="ko"): label `"👀 자세히"`, callback_data
  `"crit:click:{product_id}"`.
- English (lang=="en"): label `"👀 View"`, callback_data
  `"crit:click:{product_id}"`.

The button SHALL appear AFTER the existing 3 buttons (`crit:more`, `crit:less`,
`crit:cheap`) in the inline keyboard row layout. Exact row wrapping (1 row of
4 vs 2 rows of 2) is decided in `plan.md` based on Telegram mobile UI testing.

**Acceptance**:

- A snapshot test on `_candidate_to_card` for a non-trivial Candidate asserts
  the resulting `BotCard.critique_buttons` has exactly 4 tuples, in the
  documented order, with the documented labels per lang.
- The button's `callback_data` length SHALL be ≤ 64 bytes (Telegram limit).
  Prefix `crit:click:` is 11 chars → budget for product_id is **53 chars**.
  Current catalog (`Candidate.id`) is typically a short alphanumeric key
  (< 30 chars), well within budget. A test with a synthetic 60-char product_id
  asserts the callback_data is truncated to the **last 53 chars** of the
  product_id (preserving uniqueness at the suffix), with the truncation policy
  documented in `plan.md`. Truncated callbacks SHALL still resolve correctly
  via `Session.last_results` suffix match — exact match strategy in `plan.md`.
- A regression test asserts the existing 3 critique buttons (`crit:more`,
  `crit:less`, `crit:cheap`) still appear with unchanged labels and unchanged
  callback_data format.
- A KO/EN parity test runs the same Candidate through `_candidate_to_card` with
  `lang="ko"` and `lang="en"` and asserts both produce 4 buttons with
  language-appropriate labels.

---

## Environment Variables (introduced by this SPEC)

| Var | Required | Default | Description |
|---|---|---|---|
| `IMPLICIT_FB_ATTRIBUTION_WINDOW_S` | no | `600` | No-click attribution window in seconds. Snapshotted per impression row. REQ-FB-NOCLICK-001 / REQ-FB-MIGRATION-001. |
| `IMPLICIT_FB_NOCLICK_WEIGHT` | no | `0.2` | Soft-negative weight on no-click. REQ-FB-NOCLICK-001. |
| `IMPLICIT_FB_CLICK_WEIGHT` | no | `1.0` | Strong-positive weight on click. REQ-FB-CLICK-001. |
| `IMPLICIT_FB_REQUERY_WEIGHT` | no | `0.5` | Soft-negative weight on rapid re-query. REQ-FB-REQUERY-001. |
| `RE_QUERY_WINDOW_S` | no | `90` | Re-query detection window in seconds. REQ-FB-REQUERY-001. |

Existing vars consumed unchanged: `SESSION_CLEANUP_INTERVAL_S` (cleanup loop
period), `MEMORY_FALLBACK_ON_PROBE_FAIL` (degraded-mode detection),
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` (observability gating).

All new vars are read once at startup via `app/core/config.py::Settings` and
exposed as typed properties. Restart required for changes.

---

## Non-Goals (out of scope for this SPEC)

The following are explicitly NOT delivered by SPEC-IMPLICIT-FB-001 and MUST NOT
be conflated with it:

1. **Multi-turn vs single-turn attribution heuristics beyond the attribution
   window.** A user who clicks card 2 after seeing 5 cards in turn N, then
   continues conversation in turn N+1 — we credit only card 2's click event
   and let the other 4 expire naturally on attribution. No "this user
   considered all 5 and chose 2" inference.
2. **Cross-session signal aggregation (analytics views).** No materialized
   views, no ETL, no per-brand global CTR. This SPEC only does per-user
   reinforcement on the user's own `TasteProfile`. Aggregation is a future
   SPEC.
3. **A/B framework for weight tuning.** No experiment runner, no per-cohort
   weight selection. Weights are env-tunable for ad-hoc adjustment.
4. **Card tap "save" / "share" semantics.** No "♥ save to wishlist" / "share
   with friend" buttons. Only the "👀 자세히" click button is added.
5. **Modifying the search pipeline scoring directly.** This SPEC only updates
   `TasteProfile`. The existing pipeline (`app/pipeline/search.py`,
   `app/pipeline/diversify.py`) already reads from `TasteProfile.boost_brands`
   / `exclude_brands` / `boost_keywords` — implicit signals reach search
   automatically through that existing channel.
6. **Web UI (kikoai/app) implicit feedback.** Telegram-only for now. Web has
   no carousel + button UI to capture clicks similarly.
7. **A new Langfuse evaluator / score on traces.** No "did the user end up
   clicking" SLI dashboard, no LLM-as-judge on historical traces. Separate
   SPEC if needed.
8. **Persistent cross-session "shown" history beyond impressions.** The
   `card_impression` table is for attribution + reinforcement, not for
   "user has seen these products" deduplication (that responsibility belongs
   to `Session.shown_product_ids`, which is per-session).
9. **Telegram URL button click capture via redirect tracking.** Considered
   and rejected; "👀 자세히" callback button is the path.
10. **Per-tap deeper interactions** (e.g., long-press → "tell me more about
    this brand"). Out of scope.
11. **Reinforcement decay tuning specific to implicit signals.** The existing
    `_DECAY=0.9` in `TasteProfile` applies uniformly across explicit and
    implicit signals. Decoupling is deferred.
12. **Stop-words / blacklist in `keywords` extraction.** Whatever the existing
    extractor produces is what gets reinforced. Quality of the keyword list
    is the search pipeline's concern, not this SPEC's.
13. **GDPR / per-user opt-out from implicit tracking.** Telegram chat_id +
    from_user_id are already stored under SPEC-MEMORY-001's privacy model;
    impressions are no incremental PII surface. Per-user delete API is
    deferred.
14. **Multi-worker concurrent attribution.** SPEC-MEMORY-001 Non-Goal #9 still
    applies. `--workers 1` assumption preserved.
15. **Click-through rate (CTR) -based search ranking.** Aggregated CTR is a
    different signal class — needs cross-user aggregation. Deferred.
16. **An emergency `IMPLICIT_FB_ENABLED=false` master flag.** Could be useful
    for instant rollback under regression but adds another env var; SPEC
    deliberately omits it — invoke REQ-FB-FALLBACK-001's degraded-mode path
    (manual: point `DB_DSN` at a non-existent host) for emergency disable.
    See Open Question 3.
17. **In-line typing-indicator UX during implicit signal processing.** All
    implicit work runs in the < 50ms p99 budget — no user-facing indicator
    needed or wanted.
18. **Replaying historical impressions on a brand new bot deployment.** No
    backfill from logs.

---

## Exclusions (What NOT to Build)

(Mirrors Non-Goals — explicit list for SPEC-checker compliance.)

1. No multi-turn attribution model beyond per-impression `attribution_window_s`.
2. No cross-user analytics views or aggregation.
3. No A/B testing framework for weight tuning.
4. No "save" / "share" card affordances.
5. No changes to search pipeline scoring (only TasteProfile reinforcement).
6. No web UI implicit feedback.
7. No Langfuse evaluator scoring traces.
8. No persistent cross-session "shown" history beyond `Session.shown_product_ids`.
9. No URL-redirect click tracking.
10. No long-press or expanded card-tap interactions.
11. No implicit-specific decay rate (uniform 0.9 retained).
12. No keyword extractor stop-words / blacklist.
13. No GDPR opt-out endpoint.
14. No multi-worker concurrent attribution.
15. No CTR-based search ranking.
16. No `IMPLICIT_FB_ENABLED` master flag (use degraded mode for emergency disable).
17. No typing indicator during implicit processing.
18. No historical impression backfill.

---

## Stakeholders

| Role | Responsibility |
|---|---|
| Product / Founder (hchsa77@gmail.com) | Approves the 3 default weights (1.0 / 0.5 / 0.2) and the 600s/90s default windows. Approves the "👀 자세히" button UX addition (+1 button per card). |
| AI Server Owner (this SPEC) | All work in `app/channels/implicit_feedback.py` (NEW), `app/graphs/nodes/send_results.py` (button + impression log), `app/graphs/nodes/critique_apply.py` (click branch), `app/graphs/nodes/ingest.py` (two lazy steps), `app/core/config.py` (5 env vars), `migrations/versions/0002_create_card_impression.py` (NEW), `app/channels/session_pg.py` (cleanup DELETE clauses), all 9 test files in `tests/test_implicit_feedback/`. Owns weight tuning if defaults need adjustment. |
| dev-app Postgres operator | Verifies the new index footprint (4 indexes on a moderate write-volume table) does not regress `pg_stat_user_indexes` overall pressure. At steady-state, expect ≤ 50 inserts/day per active user × ~10 active users → trivial. |
| Langfuse operator | Verifies the new span volume (4 span types × N webhooks/day) does not blow the Langfuse self-host's storage envelope. Expected delta: ~3× current trace volume. Trace bytes per span ~0.5 KB — within headroom. |
| Modal / kikoai/app teams | Out of scope. Implicit feedback is Telegram-only and internal to kikoai/ai. |

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Lazy attribution is delayed indefinitely** for users who don't come back. Pending rows accumulate; the `idx_card_impression_chat_pending` partial index grows. | Medium | Low | REQ-FB-CLEANUP-001's second DELETE clause (60-day pending NULL → hard delete) bounds growth. At ≤ 10 active users × 5 cards × ~30 turns/month, the total row count is < 50K — well within Postgres comfort zone. The partial index excludes attributed rows. |
| R2 | **No-click weight 0.2 is too noisy and corrupts taste profiles** for casual users who scroll past without engaging despite liking the recommendation. | Medium | Medium | Weight 0.2 is intentionally small (1/5 of `crit:less`'s 1.0) precisely for this reason. `_DECAY=0.9` further diminishes its long-term impact. If field data shows poor result quality post-cutover, weight is env-tunable to 0.1 or 0.05 without code change. The acceptance test (REQ-FB-NOCLICK-001) only validates the mechanism; quality validation is a post-cutover ops concern. |
| R3 | **`ai.card_impression` foreign key to `ai.user_session` would cascade-delete legitimate impressions** when sessions are cleaned up. | n/a (mitigated by design) | High | No foreign key declared. The two tables are temporally coupled but logically independent — impressions outlive sessions intentionally so we can still attribute a session that's been cleaned up. Documented in Schema Reference. |
| R4 | **Stale `crit:click:` callback from a previous round** (user taps an old card from chat history) reinforces wrong product. | Medium | Low | REQ-FB-CLICK-001 step 1: looks up product in `Session.last_results`; if not found, silently ack with DEBUG log and no reinforcement. The session-scoped lookup is the natural staleness guard. |
| R5 | **Re-query detection false positives** for users who legitimately want to refine query (not dissatisfied) and are punished. | Medium | Medium | `RE_QUERY_WINDOW_S=90s` is the time-based heuristic — short enough to catch immediate "ugh nope, try again" but long enough that a thoughtful refinement (typing a new query) survives. Weight 0.5 is between explicit 1.0 and no-click 0.2. The signal is soft. If quality regresses, window or weight is env-tunable. |
| R6 | **`crit:click:{product_id}` callback_data exceeds Telegram's 64-byte limit** for long product_ids. | Low | Medium | Catalog `Candidate.id` is typically < 30 chars; `crit:click:` prefix is 11 chars → fits in 64 bytes for IDs ≤ 53 chars. A safety check in `_candidate_to_card` truncates to last 53 chars if longer; the lookup in `record_click` uses suffix match against `Session.last_results`. `plan.md` confirms exact strategy + unit test for 60-char ID. |
| R7 | **Impression INSERT failure during `send_results`** delays / fails the webhook response. | Low | High | REQ-FB-IMPRESSION-001 acceptance: insertion failure is caught and logged WARN; cards are STILL delivered. No user-visible failure. The lost impression row is acceptable — implicit feedback is best-effort. |
| R8 | **DB write storm**: 5 impressions × N concurrent webhooks during peak hour saturates the 10-connection pool inherited from SPEC-MEMORY-001. | Low | Medium | Single batched INSERT per `send_results` call holds the connection for < 5ms. At realistic POC volume (1-2 concurrent webhooks max), this is a non-issue. The `MEMORY_POOL_MAX_SIZE=10` headroom (SPEC-MEMORY-001 default) is intentional. |
| R9 | **Span volume regression**: 4 new spans per webhook × 30 webhooks/day = 120 extra spans/day. Langfuse storage pressure. | Low | Low | At < 0.5KB per span × 120 spans = 60KB/day. Langfuse self-host's disk headroom (per SPEC-OBSERVABILITY-002 cost analysis) absorbs this trivially. |
| R10 | **`Session.last_results`'s Candidate-shaped objects** may not carry `keywords` in a usable form — leading to empty keyword reinforcement. | Medium | Medium | The keywords extractor `_keywords_for_product` lives in `implicit_feedback.py` and is shared by `log_impressions` AND `detect_and_apply_re_query`. Both sites must agree (REQ-FB-REQUERY-001 acceptance). If extraction produces empty list, the brand-only reinforcement still happens (the disliked_brands signal alone is meaningful). |
| R11 | **The user double-taps "👀 자세히"** producing two `crit:click:` callbacks for the same product. | Medium | Low | The `UPDATE ... WHERE click_status IS NULL` is naturally idempotent at the DB level. The TasteProfile reinforcement runs twice, but `_DECAY=0.9` semantics absorb the duplicate gracefully. Documented in REQ-FB-CLICK-001 acceptance #3. |
| R12 | **`product_id` PII concern**: while product_id is catalog ref data, a privacy-sensitive product (e.g., specific lingerie brand) appearing on Langfuse spans could constitute soft-PII when joined to `chat_id_hash`. | Low | Low | `chat_id_hash` is sha256-prefix-16 — collision-resistant but not invertible without rainbow table. Product_id alone is not PII. The join is a privacy concern only on Langfuse-internal access, which is already restricted to operators. Documented; no mitigation beyond SPEC-OBSERVABILITY-002's existing access control. |
| R13 | **Re-query detection runs BEFORE the new query's vision processing**, but the `Session.last_results` reference would be a different shape (Candidate-like vs older format) after the cleanup. | Low | Medium | The reinforcement reads `Session.last_results` AS-IS at ingest entry — before any state mutation. The `_keywords_for_product` extractor is shape-tolerant (uses `getattr(c, "brand", "")` pattern from `_candidate_to_card`). Unit test covers the shape-mismatch case. |
| R14 | **Cost benchmark flakiness in CI** — the < 50ms p99 budget is tight and CI noise could spuriously fail. | Medium | Low | 20% headroom (effective threshold 60ms p99 in CI), per REQ-FB-COST-001 acceptance. Same approach as SPEC-OBSERVABILITY-002 REQ-OBS-COST-001 / R14. |
| R15 | **Migration 0002 fails on a dev DB that already has rev 0001 partially applied** (e.g., manually-created tables). | Low | Medium | `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` make the migration idempotent. Matches SPEC-MEMORY-001 pattern. |
| R16 | **Soft-negative reinforcement cumulatively wipes a user's `disliked_brands`/`disliked_keywords` slots** (capped at 50 each in `taste_profile.py::_cap`), evicting older explicit signals. | Low | Medium | The cap is enforced by descending weight. Explicit signals (weight 1.0+) outrank implicit no-click signals (0.2) by 5×, so under normal usage explicit signals dominate the top-50 retained slots. Quality monitoring post-cutover is the safety net. |

---

## Open Questions (deferred to plan.md / implementation)

These do not block SPEC approval but must be resolved before code is written:

1. **Exact `_keywords_for_product(candidate)` extraction logic.** Options: (a)
   tokenize `Candidate.subcategory + Candidate.name` and lowercase; (b)
   pre-computed `Candidate.keywords` field if it exists in the existing
   pipeline output; (c) a configurable hook. `plan.md` inspects
   `app/pipeline/search.py` and `app/pipeline/diversify.py` to determine
   what keywords-shaped data is already available on the Candidate, and uses
   the same source to avoid drift. Both `log_impressions` and
   `detect_and_apply_re_query` MUST use the same extractor.
2. **Cleanup task placement.** The DELETE clauses extend
   `PostgresSessionStore`'s existing cleanup loop. Implementation options: (a)
   inline the two new DELETE statements directly in the existing loop; (b) add
   a `cleanup_card_impressions()` helper in `implicit_feedback.py` that the
   loop calls. (b) preserves module boundaries cleanly. `plan.md` confirms.
3. **Whether to add an `IMPLICIT_FB_ENABLED` master flag.** Non-Goal #16
   explicitly omits it for now. If field testing reveals a need for instant
   rollback that goes beyond "kill the DB connection" (REQ-FB-FALLBACK-001
   degraded-mode path), `plan.md` may add a 6th env var. Decision: defer until
   evidence demands it.
4. **InlineKeyboardMarkup row layout for 4 buttons.** Options: (a) 1 row of 4
   (compact, may truncate on narrow phone screens); (b) 2 rows of 2 (more
   readable but doubles vertical space per card). `plan.md` decides based on
   Telegram mobile UI testing on actual devices.
5. **Click button label tuning.** "👀 자세히" / "👀 View" is the initial
   proposal. Alternatives: "🔗 보기" / "🔗 Open", or remove emoji entirely.
   `plan.md` confirms based on UX testing. The label SHALL be short enough that
   it does not wrap to 2 lines on the smallest target device.
6. **Whether `keywords` JSONB stores tokens or n-grams.** Existing TasteProfile
   API takes `list[str]` of normalized keywords. `_keywords_for_product` should
   produce the same shape. `plan.md` confirms.
7. **[RESOLVED 2026-05-11] `record_click`'s ack policy** — `answerCallbackQuery`
   payload remains **silent** (empty text, no toast) for BOTH the happy path
   (clicked card resolved + reinforced) AND the stale-callback path (product
   not in `last_results`). Rationale: matches Telegram-native callback default
   (silent ack), minimizes visual noise on the chat surface, and avoids
   localizing an "expired card" string in KO/EN. Resolution applies to
   REQ-FB-CLICK-001 acceptance and the DoD entry above.

---

## Cross-References

- **Builds on**:
  - SPEC-MEMORY-001 (Postgres baseline, `ai` schema, Alembic, connection pool,
    `MEMORY_BACKEND_IS_POSTGRES` detection). Revision 0002 chains from 0001.
  - SPEC-OBSERVABILITY-002 (Langfuse v3 active, `@observe` + working callback
    handler, `_hash_for_span` PII helper, v3 `update_current_span` metadata
    API).
  - SPEC-AGENT-001 (12-node LangGraph topology — extended with no new nodes,
    only logic-additions in `ingest`, `send_results`, `critique_apply`).
  - SPEC-AGENTIC-CRITIQUE-001 (explicit `crit:more` / `crit:less` / `crit:cheap`
    callback flow — extended with `crit:click:` as a fourth, semantically
    distinct branch).
  - SPEC-CLARIFY-CARDS-001 (`clarify:*` callbacks remain unchanged and are
    excluded from re-query detection).
- **Triggers / unblocks**:
  - Future SPEC: CTR-based search ranking (now has impression + click data to
    aggregate).
  - Future SPEC: Cross-user analytics views (now has the impression event
    stream).
  - Future SPEC: A/B framework for implicit weight tuning (now has the
    measurement primitive).
  - Future SPEC: Multi-turn attribution heuristics (now has the per-row
    attribution_window foundation).
- **Affected modules in kikoai/ai**:
  - NEW: `app/channels/implicit_feedback.py`,
    `migrations/versions/0002_create_card_impression.py`,
    `tests/test_implicit_feedback/test_impression_log.py`,
    `tests/test_implicit_feedback/test_click.py`,
    `tests/test_implicit_feedback/test_no_click_attribution.py`,
    `tests/test_implicit_feedback/test_re_query.py`,
    `tests/test_implicit_feedback/test_cleanup.py`,
    `tests/test_implicit_feedback/test_fallback.py`,
    `tests/test_implicit_feedback/test_cost.py`,
    `tests/test_implicit_feedback/test_observability.py`,
    `tests/test_implicit_feedback/test_config.py`.
  - MODIFIED: `app/graphs/nodes/send_results.py` (button + impression log),
    `app/graphs/nodes/critique_apply.py` (`crit:click:` branch),
    `app/graphs/nodes/ingest.py` (lazy attribution + re-query detection),
    `app/core/config.py` (5 env vars), `app/channels/session_pg.py` (cleanup
    DELETE clauses for `card_impression`), `.env.example` (5 env vars
    documented).
  - UNCHANGED (asserted): `app/channels/session.py` (Protocol + dataclass),
    `app/channels/taste_profile.py` (Protocol + dataclass + reinforce API),
    `app/providers/db_pool.py`, `app/observability/langfuse.py`,
    `app/graphs/state.py`, `app/graphs/routing.py`, `app/graphs/fashion_bot.py`,
    `app/graphs/nodes/{vision,pick_item,ask_clarify,apply_clarify,search,
    evaluator,taste_update,respond,resolve_image}.py`, `app/pipeline/**`,
    `app/api/{health,recommend,webhooks/telegram}.py`,
    `app/channels/{factory,adapter,vision,vision_prompt,clarify,clarify_values,
    lang,link_resolver}.py`, `app/channels/telegram/*`.
- **Project context**: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md`.
- **Research basis**: `docs/research/conversational-shopping-agents.md`
  takeaway #4 + Section 3 ("Implicit feedback capture — every interaction is
  a label").

---

## Definition of Done (P0)

- [ ] REQ-FB-IMPRESSION-001 implemented. Every `send_results` emit logs N rows
      (1 per sent card) into `ai.card_impression` via a single batched INSERT.
      Insertion failure does NOT block the webhook (WARN log + continue).
- [ ] REQ-FB-CLICK-001 implemented. `crit:click:{product_id}` callback routes
      through `critique_apply`'s new branch: looks up product in
      `Session.last_results`, atomically UPDATEs the impression row to
      `'clicked'`, calls `reinforce_liked_brand` and `reinforce_liked_keywords`
      with `IMPLICIT_FB_CLICK_WEIGHT`, acks the callback silently
      (`answerCallbackQuery` with empty text — no toast), does NOT reset
      `critique_*` state, does NOT send a chat message. Stale callbacks
      (product not in `last_results`) are silently acked.
- [ ] REQ-FB-NOCLICK-001 implemented. `ingest` node's lazy attribution scan
      runs as a single CTE round-trip, transitions expired-pending rows to
      `'attributed_no_click'`, reinforces each row's `(brand, keywords)` with
      `IMPLICIT_FB_NOCLICK_WEIGHT`. Failure modes silently continue (DB error
      → WARN log).
- [ ] REQ-FB-REQUERY-001 implemented. `ingest` node detects re-query
      (`Session.state==RESULTS_SENT` AND non-empty `last_results` AND
      `now() - last_active < RE_QUERY_WINDOW_S` AND not a `crit:*`/`clarify:*`
      callback), and soft-negatively reinforces every product in last_results
      with `IMPLICIT_FB_REQUERY_WEIGHT`. Runs AFTER no-click attribution,
      BEFORE the rest of ingest logic.
- [ ] REQ-FB-MIGRATION-001 implemented. Alembic revision 0002 creates
      `ai.card_impression` with 10 columns, 4 indexes (incl. 1 partial index
      on `WHERE click_status IS NULL`). Idempotent under re-run.
      `alembic upgrade head` succeeds on dev-app Postgres with revision 0001
      already applied. `alembic downgrade -1` cleanly drops the table.
- [ ] REQ-FB-CLEANUP-001 implemented. `PostgresSessionStore`'s existing cleanup
      task runs two new DELETEs every `SESSION_CLEANUP_INTERVAL_S`: attributed
      rows older than 7d (via `click_at` or `shown_at+window`), AND pending
      rows older than 60d (safety net). One INFO log per iteration with
      deleted count.
- [ ] REQ-FB-OBSERVABILITY-001 implemented. Each of the 4 functions in
      `implicit_feedback.py` emits one `implicit_feedback.{type}` Langfuse
      span with documented metadata (chat_id_hash via `_hash_for_span`, never
      raw chat_id). PII test (static + dynamic) passes.
- [ ] REQ-FB-COST-001 implemented. Benchmark test shows < 50ms p99 added
      latency vs window-disabled baseline; CI threshold 60ms p99 (20%
      headroom). Single-batched INSERT pattern verified.
- [ ] REQ-FB-FALLBACK-001 implemented. In-memory backend → all 4 functions
      silently skip (no DB writes, no WARN log, DEBUG only, spans carry
      `backend="in_memory_skipped"`). Postgres operation failure → WARN log +
      continue; webhook completes normally.
- [ ] REQ-FB-CONFIG-001 implemented. 5 env vars declared in
      `app/core/config.py::Settings` with documented defaults; 4 of them
      validated `> 0` at startup with clamp-to-default on invalid input;
      `.env.example` documents all 5.
- [ ] REQ-FB-UX-001 implemented. `_candidate_to_card` adds the 4th critique
      button `("👀 자세히" or "👀 View", f"crit:click:{product_id}")` per
      lang. The existing 3 critique buttons unchanged. callback_data ≤ 64
      bytes (truncation strategy for long product_ids documented in
      `plan.md`).
- [ ] All existing tests (`pytest -q` baseline before this SPEC, including
      SPEC-MEMORY-001 + SPEC-OBSERVABILITY-002 suites) continue to pass.
- [ ] **Coverage target (TRUST 5 Tested):** `app/channels/implicit_feedback.py`
      reports ≥ 85% line coverage. New test files in `tests/test_implicit_feedback/`
      collectively cover every public symbol of the module.
- [ ] `app/core/config.py` and `.env.example` declare all 5 new env vars with
      documented defaults and intent comments. Weight invariant
      `CLICK > REQUERY > NOCLICK` documented but not enforced.
- [ ] `migrations/versions/0002_create_card_impression.py` exists with
      `down_revision = "0001_create_memory_tables"` and DDL matches Schema
      Reference exactly (10 columns, 4 indexes, no FK).
- [ ] An end-to-end manual test against the dev Telegram bot exercises:
      (a) Send a photo → bot recommends 5 cards → in Langfuse UI verify 5
      `implicit_feedback.impression_logged` rows AND 5 `ai.card_impression`
      rows with `click_status IS NULL`.
      (b) Tap "👀 자세히" on card 2 → bot acks silently (button depresses,
      no toast, no chat message) → in Langfuse UI verify `implicit_feedback.click` span with
      `brand=...`, `weight=1.0` → query `ai.card_impression` shows row 2 has
      `click_status='clicked'`, `click_at` populated.
      (c) Wait > 600s → send new photo → in Langfuse UI verify
      `implicit_feedback.no_click` span with `attributed_count=4` (the 4
      cards that were not clicked) → `ai.card_impression` rows 1, 3, 4, 5
      now have `click_status='attributed_no_click'`.
      (d) Send photo, get cards, immediately (< 90s) send a new different
      photo → in Langfuse UI verify `implicit_feedback.re_query` span with
      `triggered=true`, `products_count=5` → `disliked_brands` /
      `disliked_keywords` in `user_taste_profile` row reflect new entries.
      (e) Set `DB_DSN` to unreachable → restart bot → `/health/ready` returns
      `memory_backend: "in_memory"` → send photo + tap → no exceptions, cards
      delivered, `implicit_feedback.click` span carries
      `backend="in_memory_skipped"`, no rows in `ai.card_impression`.
- [ ] `ruff check . && ruff format --check .` passes.
- [ ] `pytest -q` passes at the same or higher count vs the pre-SPEC baseline;
      new test count includes **9 test files** in `tests/test_implicit_feedback/`
      covering all 11 REQs with multiple test cases per file (impression_log,
      click, no_click_attribution, re_query, cleanup, fallback, cost,
      observability, config — total test case count formalized in
      acceptance.md).

---

## Implementation Plan Outline (informative — formalized in plan.md)

1. **Alembic revision 0002**: `alembic revision -m "create card_impression
   table"`, hand-write the DDL with 4 indexes; `alembic upgrade head` on local
   dev Postgres.
2. **`implicit_feedback.py` module**: 4 public functions (`log_impressions`,
   `record_click`, `attribute_expired_impressions`,
   `detect_and_apply_re_query`) + shared helpers (`_keywords_for_product`,
   `_taste_for`, `_hash_chat_id_for_span` proxy to SPEC-OBSERVABILITY-002's
   `_hash_for_span`). Each public function wrapped with
   `@observe(name="implicit_feedback.{type}", as_type="span")`. Module-level
   `_BACKEND_IS_POSTGRES` boolean set during lifespan startup.
3. **`send_results.py` modification**: 4th button in `_candidate_to_card`;
   `log_impressions` call after `get_store().update(sess)`.
4. **`critique_apply.py` modification**: new branch for `crit:click:` callback
   data prefix. Carefully separate from the existing `crit:more` / `crit:less` /
   `crit:cheap` reset-and-route logic.
5. **`ingest.py` modification**: two new lazy steps at node entry —
   `attribute_expired_impressions` first, `detect_and_apply_re_query` second.
   Both wrapped in try/except (WARN + continue on failure).
6. **`session_pg.py` cleanup task modification**: add two DELETE clauses (or
   call into `implicit_feedback.cleanup_card_impressions(pool)` helper).
7. **Config + .env.example**: 5 new env vars with defaults.
8. **Tests**: 9 test files in `tests/test_implicit_feedback/`. testcontainers
   Postgres-based integration; mock Langfuse for observability; mock adapter
   for UI flow.
9. **Cutover**: deploy revision 0002 on dev-app Postgres → deploy code →
   smoke-test the 5 end-to-end scenarios → monitor Langfuse for 24h →
   inspect `ai.card_impression` for sanity.

---

## Test Plan Outline (informative — formalized in acceptance.md)

- **Unit (`tests/test_implicit_feedback/test_impression_log.py`)**: batched
  INSERT shape; per-row column population; failure-mode catches.
- **Unit (`tests/test_implicit_feedback/test_click.py`)**: branch routing in
  `critique_apply`; UPDATE idempotency on double-tap; stale-callback silent
  ack; TasteProfile reinforcement weight assertion; no critique_* state
  reset.
- **Unit (`tests/test_implicit_feedback/test_no_click_attribution.py`)**:
  single CTE round-trip; transition pending → attributed_no_click only when
  expired AND pending; reinforcement weight assertion; partial-index usage
  via EXPLAIN.
- **Unit (`tests/test_implicit_feedback/test_re_query.py`)**: window-boundary
  detection; state-precondition gating (RESULTS_SENT only); callback-excluded
  case; ordering vs no-click attribution.
- **Unit (`tests/test_implicit_feedback/test_cleanup.py`)**: two DELETE
  clauses; 7d clicked / 7d attributed / 60d pending; idx_card_impression_cleanup
  EXPLAIN.
- **Unit (`tests/test_implicit_feedback/test_fallback.py`)**: in-memory backend
  → silent skip + DEBUG-only logs + span carries
  `backend="in_memory_skipped"`; psycopg OperationalError → WARN log + no
  exception propagation.
- **Unit (`tests/test_implicit_feedback/test_cost.py`)**: 500-iteration
  benchmark with seeded expired rows; baseline vs measurement delta < 50ms
  p99 (60ms CI threshold).
- **Unit (`tests/test_implicit_feedback/test_observability.py`)**: each of 4
  functions emits one span with documented metadata; PII static + dynamic
  tests (no raw chat_id in payload).
- **Unit (`tests/test_implicit_feedback/test_config.py`)**: 5 settings exist
  with documented defaults; `> 0` clamp on 4 of them with WARN log; weight
  invariant documented (not enforced).
- **Integration**: send_results → impression log → click on card 2 → DB row
  transition → attribution scan finds 4 expired → re-query detection on next
  turn. Full end-to-end with testcontainers Postgres + mock Langfuse + mock
  Telegram adapter.
- **Regression**: full existing `tests/` tree green under both
  `MEMORY_FALLBACK_ON_PROBE_FAIL=true` (in-memory: implicit feedback inert)
  and `=false` (postgres: implicit feedback active).
- **Coverage**: `pytest --cov=app.channels.implicit_feedback` reports ≥ 85%.
- **End-to-end manual**: the 5 scenarios in the Definition of Done section.
