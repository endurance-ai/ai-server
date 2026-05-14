---
id: SPEC-CONVERSATION-LOG-001
version: 0.1.0
status: draft
created: 2026-05-14
updated: 2026-05-14
author: hchsa77@gmail.com
priority: P0
issue_number: null
labels: [event-sourcing, observability, data-moat, postgres, jsonb, telegram, ml-dataset, behavior-analytics, debug-replay]
---

# SPEC-CONVERSATION-LOG-001: Append-Only Conversation Event Log (Data Moat for Behavior Analytics, ML, and Debug Replay)

## HISTORY

- 2026-05-14 (v0.1.0): 초안 작성. 직접적 동기는 `docs/_tmp/noscroll-benchmark.html` 의 noscroll 벤치마크 리서치 — "사용자 행동 시퀀스(어떤 추천 → 어떤 클릭 → 어떤 재질문)는 본 프로젝트의 가장 강력한 데이터 해자(moat)이며, 이를 기록하지 않는 것은 모델 개선의 가장 큰 손실"이라는 관찰. 현 상태에서 영속 데이터는 (a) `ai.user_taste_profile` (mutable snapshot, SPEC-MEMORY-001), (b) `ai.user_session` (mutable snapshot, TTL, SPEC-MEMORY-001), (c) `ai.card_impression` (append-only, 카드 노출/클릭 attribution 전용, SPEC-IMPLICIT-FB-001) 세 테이블뿐. Langfuse v3 trace 는 30일 보존이라 SQL 분석/ML 데이터셋/장기 replay 에 부적합하고, 봇 stdout 로그는 휘발성. 본 SPEC 은 이 갭을 메우기 위해 **단일 append-only 이벤트 소싱 테이블** `ai.log_conversation_event` 를 도입하고, LangGraph 12 노드(SPEC-AGENT-001) 와 Telegram webhook 인테이크 지점에서 **fire-and-forget** 으로 이벤트를 emit. 사용자가 컨버세이션 라운드에서 직접 확정한 정책 결정: (A) 단일 테이블 이벤트 소싱 (per-event-type 테이블 분리는 기각); (B) PII 원본 저장 (해싱/리덕션 없음); (C) **영구 보존** (cold storage tier / TTL cron 없음, GDPR 삭제는 user_key 기반 ad-hoc DELETE 로 처리); (D) per-node fire-and-forget (`asyncio.create_task`), 이벤트 쓰기 실패 시 그래프 중단 금지; (E) 목적 3종 동시 — 행동 분석 + ML 데이터셋 + 디버그 replay, 그러므로 payload schema 는 세 가지 사용처를 모두 만족할 만큼 풍부해야 함. 본 SPEC 은 SPEC-MEMORY-001 (`ai` 스키마 + psycopg3 풀), SPEC-OBSERVABILITY-002 (Langfuse trace_id 크로스 레퍼런스), SPEC-IMPLICIT-FB-001 (`card_impression` 과의 보완 관계 — replace 가 아닌 add), SPEC-ONBOARD-CARDS-001 (`onboard_select` 이벤트 타입), SPEC-AGENT-001 (12 노드 토폴로지 — 각 노드가 이벤트 emitter 가 됨) 위에 쌓이며, 이 중 어떤 SPEC 도 수정하지 않는다.

---

## Goal

현재 kiko.ai Telegram 패션 봇은 **무엇이 일어났는지** 를 영구적으로 기록하지 않는다. 영속 저장소는 세 군데뿐이고 각각 다음의 협소한 책임을 진다:

| 테이블 | 책임 | 한계 |
|---|---|---|
| `ai.user_taste_profile` (SPEC-MEMORY-001) | 사용자별 brand/keyword 감쇠 가중치 **스냅샷** | mutable, 이전 상태 비복원 |
| `ai.user_session` (SPEC-MEMORY-001) | chat 별 진행 상태 + `last_results` **스냅샷**, TTL 30분 | mutable, TTL 만료 후 소실 |
| `ai.card_impression` (SPEC-IMPLICIT-FB-001) | append-only **이지만** 카드 노출/클릭 attribution 전용 (10 컬럼) | 의도/검색쿼리/critique/vision 결과 미기록 |

세 테이블 모두 다음 질문에 답할 수 없다:

1. **"사용자 A 가 어제 카드 X 를 봤을 때 어떤 search query 가 들어갔는가?"** — search query 는 휘발됨.
2. **"이 추천이 왜 등장했는가? 어떤 vision 결과, 어떤 RRF 점수로?"** — `last_results` 에 candidate 만 남고 score / 필터 로그는 없음.
3. **"지난주 클릭 결과만 가지고 (query → top-K → click) 시퀀스로 ML 재랭킹 데이터셋을 뽑고 싶다."** — query 와 top-K 가 같은 키로 묶이지 않음.
4. **"critique loop 가 2회 돌았는데 첫 시도와 두 번째 시도의 결과가 무엇이 달랐는가?"** — iteration 별 결과 미기록.

Langfuse v3 (SPEC-OBSERVABILITY-002) 가 LLM/agent call tree 를 trace 하지만:

- **30일 보존** (self-host 디스크 한계 + SPEC-OBSERVABILITY-002 cost 가드).
- **trace tree 시각화 용도** — SQL group-by / window function 분석 불가.
- **structured payload 표준화 없음** — span metadata 가 ad-hoc, ML dataset extract 시 비정형.
- **재현(replay) 불가** — span 은 LLM 호출 spec 을 그대로 보존하지 않음 (예: search RPC 의 raw 응답).

봇 컨테이너 stdout 로그도 휘발성 + grep-only — 데이터 자산이 아니라 디버그 가시화일 뿐.

`docs/_tmp/noscroll-benchmark.html` 의 noscroll 벤치마크 리서치는 정확히 이 갭을 지적한다 — **사용자 행동 시퀀스가 본 프로젝트의 가장 강력한 해자**이며, 이를 영구 기록하지 않으면 (a) 어떤 추천 패턴이 전환되는지 측정 불가, (b) ML 재랭킹 모델용 (query → top-K → click) 데이터셋 구축 불가, (c) "왜 이 추천이 등장했나" 사후 디버그 replay 불가 — 세 가지 사용처가 모두 차단된다.

본 SPEC 은 이 갭을 메우는 단일 append-only 이벤트 소싱 테이블 `ai.log_conversation_event` 를 도입한다. 모든 LangGraph 노드와 Telegram webhook 인테이크 지점이 의미 있는 user-bot 인터랙션마다 **하나 이상의 row 를 emit** 한다. 핵심 설계 원칙:

1. **Single-table event sourcing.** Per-event-type 테이블 분리(option B) 는 사용자가 명시적으로 기각 — 테이블 폭증을 막고 SQL 분석 시 단일 `FROM ai.log_conversation_event` 로 시작할 수 있게 한다. `event_type` 컬럼이 row 변별자, `payload` JSONB 가 가변 스키마를 담는다.
2. **Fire-and-forget per node.** 모든 emit 은 `asyncio.create_task(log_event(...))` 패턴. 이벤트 쓰기 실패는 사용자 응답을 절대 방해하지 않는다 — log_event 자체가 모든 예외를 흡수하고 그래프는 **무조건** 계속 진행한다 (REQ-LOG-FAILSOFT-001).
3. **PII raw, not redacted.** 사용자가 명시적으로 raw 저장을 선택. 텍스트, URL, 사진 R2 URL, Pinterest URL — 전부 원본. 해싱/리덕션/마스킹 없음. GDPR/삭제 요청은 `DELETE FROM ai.log_conversation_event WHERE user_key=$1` 로 ad-hoc 처리 (REQ-LOG-PRIVACY-001).
4. **Permanent retention.** TTL cron 없음, cold storage tier 없음, 자동 삭제 없음. 디스크 footprint 가 부담되면 향후 SPEC 에서 분리하되, 본 SPEC 단계에서는 **모든 row 가 영원히 산다** (REQ-LOG-RETENTION-001).
5. **Thread-based correlation.** 한 사용자 턴 사이클(ingest → respond)은 `thread_id: UUID` 로 묶인다. ingest 노드가 seed, 12 노드가 같은 thread_id 를 전파, respond 가 마지막으로 사용. 그래프가 끝나도 thread 는 닫히지 않는다 — `turn_no` 가 thread 내 단조 증가 카운터로 노드 순서를 기록. SQL group-by 시 thread_id 가 1차 키.
6. **Three purposes, one payload schema.** payload 는 (i) 행동 분석 (SQL aggregate), (ii) ML 데이터셋 (query → top-K → click 시퀀스 추출), (iii) 디버그 replay (왜 이 추천이?) 세 가지 모두 만족해야 한다. 그러므로 `search_done` payload 는 top-K product_id 와 RRF score 를 parallel array 로 같이 담고, `vision_done` 은 v2 schema 전체를 그대로, `evaluator_run` 은 iteration 별 score + delta + retry decision 까지 보존한다 (REQ-LOG-PAYLOAD-RICH-001).
7. **Langfuse cross-reference, not replacement.** `langfuse_trace` 컬럼은 같은 turn 의 Langfuse trace_id 를 담는다. Langfuse 가 살아 있는 30일 동안은 trace tree 시각화를, 그 후로는 본 테이블의 영구 raw 데이터를 — 둘은 보완 관계이고 본 SPEC 은 Langfuse 를 대체하지 않는다 (REQ-LOG-LANGFUSE-XREF-001).
8. **Complement to `card_impression`, not replacement.** SPEC-IMPLICIT-FB-001 의 `card_impression` 은 노출/클릭 attribution 전용 (10 컬럼) 으로 계속 쓰인다. 본 SPEC 의 `card_sent`/`card_clicked` 이벤트는 추가 기록 — `card_impression` 이 reinforcement attribution 의 정답 source-of-truth 로 남고, `log_conversation_event` 는 같은 일의 timeline 표현. 의도된 중복(REQ-LOG-IMPLICIT-FB-COEXIST-001, R-DUP).

이 SPEC 은 **WHAT** 과 **WHY** 만 정의한다. 정확한 데코레이터 모양, log_event 호출이 노드 본문 안인지 LangGraph hook 인지, asyncio.Task 모니터링 패턴, JSONB 인코더 cascade 의 정확한 fallback 순서 등 **HOW** 는 `plan.md` 와 Run phase 에서 결정한다.

이 마이그레이션은 **새로운 영속 표면 추가** 이지 기존 그래프 토폴로지 변경이 아니다. 외부 사용자 행위(메시지, 카드, KO/EN, clarify 흐름)는 byte-identical 하게 유지된다.

---

## Background

### 잃어버리는 신호 vs 현재 캡처되는 신호

| 신호 | 현재 캡처? | 어디에? | 본 SPEC 이 추가하는가? |
|---|---|---|---|
| 사용자 인바운드 텍스트 (`/start`, "어제 사진처럼", "ami 좋아해") | ✗ | (stdout 로그만) | ✓ `user_text` |
| 사용자 인바운드 사진/Pinterest URL | 부분 (attachment_id 만 Session) | mutable session | ✓ `user_photo`, `link_resolved` |
| Vision v2 결과 (style_node, mood, palette, items) | 부분 (Session.vision_result, mutable) | TTL 30분 만료 후 소실 | ✓ `vision_done` (영구) |
| Router 의도 결정 (new_search vs continue) | ✗ | (휘발) | ✓ `intent_routed` |
| Pick item 다중 disambiguation 결과 | ✗ | (휘발) | ✓ `pick_item_done` |
| Clarify 카드 노출 + 콜백 | 부분 (`Session.clarify_axis/value` snapshot) | mutable | ✓ `ask_clarify_sent`, `clarify_applied` |
| Search RPC raw 결과 (top-K product_ids + RRF scores + filter drop) | ✗ | (휘발) | ✓ `search_done` (← ML 데이터셋 핵심) |
| Diversify 결과 (input/output count + 캡) | ✗ | (휘발) | ✓ `diversify_done` |
| Critique loop 매 iteration | 부분 (Langfuse span, 30일) | 30일 후 소실 | ✓ `evaluator_run` (영구) |
| 카드 노출 | ✓ | `ai.card_impression` (SPEC-IMPLICIT-FB-001) | ✓ `card_sent` (timeline view 보완 — 의도된 중복) |
| 카드 클릭 | ✓ | `ai.card_impression` | ✓ `card_clicked` (timeline view 보완) |
| 봇 자연어 응답 (chunked) | ✗ | (stdout) | ✓ `bot_text` |
| TasteProfile 갱신 이벤트 (click/critique/onboard 등 어떤 소스에서?) | ✗ (결과만 user_taste_profile snapshot) | mutable, source 추적 불가 | ✓ `taste_update` (source 명시) |
| Onboarding 카드 선택 | ✗ | (휘발) | ✓ `onboard_select` |
| Pinterest 일괄 처리 (board/profile/pin) | ✗ | (stdout) | ✓ `pinterest_ingest` |
| 노드 실행 중 예외 | ✗ | (stdout) | ✓ `node_error` |

본 SPEC 은 위 17 개 이벤트 타입(`user_callback` 포함)으로 캡처 범위를 확정한다. 향후 노드가 추가되면 새 이벤트 타입을 추가할 수 있도록 schema 는 `event_type TEXT` 로 open-ended.

### 왜 단일 테이블인가 (option A) — 사용자 결정

사용자는 본 라운드에서 명시적으로 option A 를 선택했다. 비교는:

| 옵션 | 장점 | 단점 | 결정 |
|---|---|---|---|
| **(A) 단일 `log_conversation_event` (JSONB payload)** | 1테이블만 관리, SQL 분석 진입점 단일, schema evolution 유연 | type-specific 제약 강제 불가 (앱 레이어 책임), 인덱싱 일부 GIN 의존 | **채택** |
| (B) `log_vision_done` / `log_search_done` / `log_card_sent` … 17개 테이블 | 컬럼 단위 타입 안전, JOIN 시 typed | 테이블 폭증, 마이그레이션 17개, JOIN 비용 | **기각** |

사용자 코멘트(요지): "테이블이 17개로 늘어나는 건 분명히 ML 추출 시 더 깔끔하지만 운영 부담이 너무 크고, JSONB GIN 으로 90% 의 쿼리는 충분히 빠르다. 정 안 되면 그때 view 로 split 하자."

향후 데이터 양이 늘어 JSONB GIN 으로도 못 버틸 때 split 은 별도 SPEC. 본 SPEC 은 그 가능성을 R3 (storage growth) 에서 문서화하고 진로를 열어둔다.

### 왜 fire-and-forget 인가 — 사용자 결정

봇의 응답 latency 는 사용자 경험의 가장 큰 변수다. 이벤트 로깅이 응답을 1ms 라도 늦추면 정당화 불가. 옵션:

| 옵션 | 비용 | 신뢰성 |
|---|---|---|
| **(A) fire-and-forget (`asyncio.create_task` + exception swallow)** | ~0ms (스케줄 비용만) | 컨테이너 크래시 시 in-flight task 손실 가능 — 받아들임 |
| (B) Per-node sync write | DB round-trip × 노드 수 만큼 응답 늦어짐 | 100% 보장 |
| (C) Per-turn batched write at respond | 응답 직전 1회 — 비교적 저렴 | 그래프 중도 종료 시 turn 전체 손실 |

사용자는 (A) 를 채택. 받아들이는 trade-off: 봇이 SIGKILL 로 죽으면 직전 N 초의 task 가 손실됨. **본 SPEC 은 eventually-consistent 가 아니라 best-effort** 임을 명시하며 (R5), `user_taste_profile` / `user_session` 같은 mutable snapshot 이 본 SPEC 과는 별개로 100% 일관성을 보장한다.

### 왜 영구 보존인가 — 사용자 결정

본 라운드에서 사용자가 명시적으로 선언:

> "지워지면 안 됨. cold storage tier 도 안 둠. 정 디스크가 부족하면 그때 SPEC 추가하자. 일단은 영원히 산다."

GDPR/사용자 삭제 요청은 **ad-hoc per-user-key DELETE** 로 처리한다 — `DELETE FROM ai.log_conversation_event WHERE user_key=$1` 단일 statement. user_key 가 PK 의 일부가 아니라 indexed column 이라 비싸지 않다 (`idx_log_conv_user_time`).

### 왜 PII 원본 저장인가 — 사용자 결정

해싱/리덕션을 적용한 다음에야 ML 데이터셋이 의미를 갖기 어렵다 — 텍스트 쿼리는 원본으로 보존되어야 검색 임베딩 재훈련에 쓰일 수 있다. 사용자가 본 라운드에서:

> "PII raw 로. 어차피 내가 가진 데이터, 나만 본다. 해싱은 안 함. 단, 사용자가 지워달라고 하면 즉시 지워줄 수 있어야 함."

따라서 본 SPEC 은 row-level DELETE 를 user_key 인덱스로 빠르게 지원하지만 (REQ-LOG-PRIVACY-001), payload 내 텍스트/URL 은 raw 저장 (R-PII).

### 왜 세 가지 목적인가 — payload schema 설계의 함의

사용자는 본 라운드에서 세 가지 사용처를 모두 동시에 만족해야 한다고 명시했다:

1. **행동 분석 (analytics)**: 예) "주중 vs 주말 클릭률 차이", "ami 브랜드를 본 후 클릭한 사용자 비율" — 주로 group-by, count, percentile.
2. **ML 데이터셋 구축**: 예) `(user_key, query_text, top_k_product_ids, rrf_scores, clicked_product_ids)` 시퀀스를 시간 순서로 뽑아 재랭킹 모델 학습.
3. **디버그 replay**: 예) "어제 ami 사용자의 'noscroll' 추천이 왜 등장했나?" — search query, vision input, evaluator iteration 까지 추적.

세 목적이 같은 payload schema 를 공유하려면:

- 행동 분석용: 단순 카운트 가능한 unit dimension (intent, axis, position).
- ML 용: query, top_k, rrf_scores 같은 raw signal 을 같은 thread_id 로 묶을 수 있어야 함.
- Replay 용: input → output 인과 추적이 가능하도록 vision 결과, search 입력, critique iteration 까지 보존.

→ payload 가 풍부해야만 한다 (REQ-LOG-PAYLOAD-RICH-001). 본 SPEC 의 17 이벤트 타입은 위 세 가지 목적의 합집합을 cover.

### 왜 `card_impression` 과 공존하는가 — 의도된 중복

SPEC-IMPLICIT-FB-001 의 `ai.card_impression` 은 **카드 노출 attribution 전용** — `click_status` 컬럼이 NULL → 'clicked' / 'attributed_no_click' 으로 전이하며 TasteProfile reinforcement 의 source-of-truth 가 된다.

본 SPEC 의 `card_sent` / `card_clicked` 이벤트는 **같은 사건의 timeline view** — append-only, 상태 전이 없음, payload 가 RRF score / position 등 timeline 분석용 보조 정보를 함께 담는다.

| 질문 | 답이 있는 곳 |
|---|---|
| "지금 이 카드의 attribution status 는?" | `card_impression` (mutable, single row per impression) |
| "지난 7일 동안 사용자별 1회 노출당 평균 클릭 position 은?" | `log_conversation_event` (append-only, time series) |
| "이 카드 노출이 어떤 search query 결과에서 왔는가?" | `log_conversation_event` (`search_done` 와 thread_id 로 JOIN) |

중복 비용:

- 디스크: card 당 1 row × 2 테이블 = 2배. 카드 5개/turn × 100 turn/day × 365일 ≈ 36만 row/year. JSONB 2KB 가정 시 ~700MB/year. 부담 없음.
- 일관성: 두 테이블이 어긋날 수 있음. 본 SPEC 은 timeline 으로서의 정확성만 보장 (`card_impression` 의 click_status 와 본 SPEC 의 `card_clicked` 가 동일 시점에 emit 되지 않을 수 있다는 점 받아들임 — REQ-LOG-IMPLICIT-FB-COEXIST-001).

`card_impression` 을 본 SPEC 의 view 로 흡수하는 옵션도 검토했으나, SPEC-IMPLICIT-FB-001 의 attribution 로직(`UPDATE … WHERE click_status IS NULL`) 이 mutable single-row 를 전제로 짜여 있어 옮기면 SPEC-IMPLICIT-FB-001 의 acceptance 가 깨진다. → 공존 채택.

### SPEC-MEMORY-001 / SPEC-IMPLICIT-FB-001 / SPEC-OBSERVABILITY-002 / SPEC-ONBOARD-CARDS-001 / SPEC-AGENT-001 와의 관계

- **의존 (필수)**: SPEC-MEMORY-001. `ai` 스키마, Alembic baseline (rev 0001), psycopg3 AsyncConnectionPool (`app/providers/db_pool.py::get_pool()`), `MEMORY_BACKEND_IS_POSTGRES` 가드. 본 SPEC 은 새 풀을 만들지 않고 기존 풀을 재사용.
- **의존 (필수)**: SPEC-AGENT-001. 12 노드 토폴로지 — 본 SPEC 이 12 노드 모두에 emit 호출을 박는다.
- **의존 (소프트)**: SPEC-IMPLICIT-FB-001. `card_impression` 이 active 라는 전제 하에서 `card_clicked` 이벤트가 `crit:click:*` 콜백 경로에 박힘. SPEC-IMPLICIT-FB-001 이 없어도 본 SPEC 은 동작하지만 `card_clicked` 이벤트 발화 빈도가 0.
- **의존 (소프트)**: SPEC-OBSERVABILITY-002. Langfuse v3 active 시 `langfuse_trace` 컬럼이 채워진다. 비활성 (no-op fallback) 시 NULL — 본 SPEC 은 Langfuse 가 없어도 100% 동작.
- **의존 (소프트)**: SPEC-ONBOARD-CARDS-001. 온보딩 카드 흐름이 추가될 때 `onboard_select` 이벤트가 발화. 본 SPEC 은 그 이벤트 타입을 미리 카탈로그에 등재.
- **무관**: SPEC-PIPELINE-001, SPEC-VISION-UNIFY-001, SPEC-AGENTIC-CRITIQUE-001, SPEC-CLARIFY-CARDS-001 — 이 SPEC 들의 노드는 본 SPEC 의 emit 호출만 추가될 뿐 로직은 무변경.

---

## Architecture Snapshot (informative)

Today (pre-SPEC):

```
user message arrives
  ↓
Telegram webhook → ingest → router → vision → … → search → evaluator → send_results → respond
                                                                                          ↓
                                                                                    user sees cards

영속화되는 것:
  - user_taste_profile (mutable snapshot)
  - user_session (mutable snapshot, TTL 30분)
  - card_impression (append-only, 10 컬럼, 카드 1개당 1 row)
  - Langfuse trace tree (30일 보존)

휘발되는 것:
  - 사용자 인바운드 텍스트 본문, 사진 URL, callback_data
  - Vision 결과 전체 (Session.vision_result 도 30분 후 소실)
  - Router intent 결정
  - Search RPC 의 query, top-K, RRF score, filter drop log
  - Diversify 결과
  - Critique iteration 별 score / delta / retry decision
  - 봇 자연어 응답 chunk
  - TasteProfile 갱신의 source (어떤 신호로 갱신됐는지)
  - Pinterest 일괄 처리 metadata
  - 노드 예외
```

After this SPEC:

```
Telegram webhook
  ↓
  ├─ emit(user_text | user_photo | user_callback) ─ fire-and-forget ──► ai.log_conversation_event
  │   (thread_id = uuid4() seed, turn_no = 0)
  ↓
ingest [Step 1: lazy attribution scan from SPEC-IMPLICIT-FB-001]
  │  [Step 2: re-query detection from SPEC-IMPLICIT-FB-001]
  │  [Step 3: NEW — propagate thread_id + bump turn_no on WorkingState]
  │  emit(intent_routed) ──► ai.log_conversation_event (turn_no = 1)
  ↓
resolve_image (Pinterest/pin.it/og:image)
  │  emit(link_resolved | pinterest_ingest) ──► (turn_no = 2)
  ↓
vision (LiteLLM, schema_v2)
  │  emit(vision_done) ──► (turn_no = 3)
  ↓
pick_item (if multi-item)
  │  emit(pick_item_done) ──► (turn_no = 4)
  ↓
ask_clarify (if weak vision)
  │  emit(ask_clarify_sent) ──► (turn_no = 5)
  │
  ↓ (or apply_clarify on callback re-entry)
  │  emit(clarify_applied)
  ↓
search (search_products_v5 RPC)
  │  emit(search_done) ──► payload: query, embedding_ref, top_k[], rrf_scores[], dense_count, sparse_count, filter_drop_log
  ↓
evaluator (critique loop, up to 2 iterations)
  │  emit(evaluator_run) per iteration ──► payload: iteration_no, score, delta, retry_decision, exhausted
  ↓
diversify (브랜드/플랫폼 캡)
  │  emit(diversify_done) ──► payload: input_count, output_count, brand_cap, platform_cap
  ↓
send_results (Telegram dispatch)
  │  emit(card_sent) per card ──► payload: product_id, position, send_ok, send_elapsed_ms
  │  (existing: ALSO writes ai.card_impression — SPEC-IMPLICIT-FB-001 — coexistence)
  ↓
respond (chunked bot reply)
  │  emit(bot_text) per chunk ──► payload: chunk_text, chunk_index, total_chunks, flow
  ↓
taste_update (when triggered: click / onboard / pinterest / critique)
  │  emit(taste_update) ──► payload: source, keywords_delta, brands_delta

Any node raises → except handler → emit(node_error) ──► payload: node_name, exception_type, message, recovered

Async return path (user taps "👀 자세히" — SPEC-IMPLICIT-FB-001):
Telegram webhook → emit(user_callback)
                ↓
              critique_apply (crit:click branch)
                │  emit(card_clicked) ──► payload: product_id, position, dwell_ms
                │  (existing: ALSO updates ai.card_impression.click_status)

Background (no new task — uses existing pool):
None. No cleanup, no TTL, no archive. Rows live forever (REQ-LOG-RETENTION-001).
```

**Affected modules in kikoai/ai (this SPEC — informational; exact filenames refined in `plan.md`)**:

- `migrations/versions/0004_create_log_conversation_event.py` — NEW. Alembic revision (down_revision points at the most recent existing revision; 0003 number reserved if implicit-fb introduces 0003 first — `plan.md` confirms the exact chain). Creates `ai.log_conversation_event` + 4 indexes.
- `app/observability/conversation_log.py` — NEW. 모듈 본체:
  - `async def log_event(user_key: str, chat_id: int, thread_id: UUID, turn_no: int, event_type: str, payload: dict, langfuse_trace: str | None = None, latency_ms: int | None = None) -> None`
  - 시그니처는 fire-and-forget 의 의미를 함축 (반환값 없음, 예외 흡수).
  - 내부 동작: `MEMORY_BACKEND_IS_POSTGRES` flag 가 False → DEBUG 한 줄 + return (silent skip, REQ-LOG-FAILSOFT-001). True → `get_pool().connection()` 빌려 single INSERT, 모든 예외 catch → WARN 한 줄 후 return.
  - 보조 함수: `current_langfuse_trace_id() -> str | None` (SPEC-OBSERVABILITY-002 의 v3 client 에서 trace_id 끌어옴; 비활성 시 None).
  - 보조 함수: `seed_thread() -> UUID` (uuid4 alias — webhook 진입점에서 호출).
  - **모든 emit 은 호출자가 `asyncio.create_task(log_event(...))` 로 감싼다.** 모듈 본체는 그 자체로는 task 생성하지 않음 (호출자 책임 — 디자인 단순화).
  - 단, REQ-LOG-FAILSOFT-001 의 stderr fallback 은 모듈 내부에서 처리한다.
- `app/graphs/state.py` — MODIFIED. `InputState` / `WorkingState` 에 두 필드 추가:
  - `thread_id: UUID = Field(default_factory=uuid4)` — webhook entry 가 직접 seed 하거나 ingest 가 default 채움.
  - `turn_no: int = 0` — 각 노드가 emit 직전에 bump (또는 ingest 가 0 으로 reset).
  - 두 필드는 `OutputState` 에 propagate 되지 않아도 됨 (그래프 종료 후엔 의미 없음 — `respond` 까지 살아 있으면 충분).
- `app/api/webhooks/telegram.py` — MODIFIED. 그래프 invocation 직전에 thread_id seed + 인바운드 종류별 emit (`user_text` / `user_photo` / `user_callback`). turn_no=0 (ingest 직전 단계).
- `app/graphs/nodes/ingest.py` — MODIFIED. 노드 끝에서 `intent_routed` emit (turn_no=1). thread_id 는 InputState 에서 이미 들어 있음 — propagate 만.
- `app/graphs/nodes/resolve_image.py` — MODIFIED. `link_resolved` 또는 `pinterest_ingest` emit (turn_no bump).
- `app/graphs/nodes/vision.py` — MODIFIED. `vision_done` emit. payload 는 v2 schema 전체 (styleNode / mood / palette / items[]) — Vision LLM 응답 그대로.
- `app/graphs/nodes/pick_item.py` — MODIFIED. `pick_item_done` emit.
- `app/graphs/nodes/ask_clarify.py` — MODIFIED. `ask_clarify_sent` emit.
- `app/graphs/nodes/apply_clarify.py` — MODIFIED. `clarify_applied` emit.
- `app/graphs/nodes/search.py` — MODIFIED. `search_done` emit. **payload 는 본 SPEC 의 ML 데이터셋 사용처의 핵심** — query, embedding_ref(가능하다면 hash), top_k_product_ids[], rrf_scores[], dense_count, sparse_count, filter_drop_log.
- `app/graphs/nodes/evaluator.py` — MODIFIED. iteration 당 `evaluator_run` 1회 emit. payload 는 iteration_no, score, delta, retry_decision, exhausted.
- `app/graphs/nodes/send_results.py` — MODIFIED. 카드당 `card_sent` 1회 emit (기존 `ai.card_impression` INSERT 와 별도, 같은 노드 내). diversify 결과는 `diversify_done` 으로 직전 단계에 emit (현재 노드 경계가 send_results 안에 있다면 그 안에서, 그래프 토폴로지 변경 없음).
- `app/graphs/nodes/respond.py` — MODIFIED. chunk 당 `bot_text` emit. flow 분류 (`respond` enum 그대로) payload 에.
- `app/graphs/nodes/taste_update.py` — MODIFIED. `taste_update` emit. payload 의 `source` 는 "free_text" (현 노드의 진입 사유).
- `app/graphs/nodes/critique_apply.py` — MODIFIED. **두 가지 추가**: (a) `crit:click:*` 콜백 처리 시 `card_clicked` emit, (b) `crit:more/less/cheap` 처리 시 `taste_update` emit with `source="critique"`.
- `app/observability/langfuse.py` — MODIFIED (minor). `current_langfuse_trace_id()` 헬퍼 export — v3 `get_current_trace_id()` 또는 동치 API 래핑. v2/no-op fallback 시 None.
- `app/main.py` — MODIFIED (minor). lifespan 안에서 `MEMORY_BACKEND_IS_POSTGRES` flag 가 set 된 후 conversation_log 모듈에 같은 flag 를 reuse 하도록 보장 (모듈 import 만 추가; flag 는 SPEC-MEMORY-001 의 module-level state 를 그대로 읽음).
- `tests/test_conversation_log/test_log_event.py` — NEW. log_event 의 happy path + 예외 흡수 + in-memory fallback skip.
- `tests/test_conversation_log/test_thread_propagation.py` — NEW. webhook → ingest → … → respond 전 구간에서 thread_id 가 같은 값으로 유지되는지, turn_no 가 단조 증가하는지.
- `tests/test_conversation_log/test_payload_shapes.py` — NEW. 17 이벤트 타입 각각의 payload 가 documented schema 와 일치 (key 존재 + type).
- `tests/test_conversation_log/test_search_payload.py` — NEW. `search_done` payload 가 top_k_product_ids[] 와 rrf_scores[] 를 같은 길이로 보존.
- `tests/test_conversation_log/test_node_error.py` — NEW. 노드 강제 raise 시 `node_error` row 가 기록되고 그래프는 계속 진행.
- `tests/test_conversation_log/test_failsoft.py` — NEW. log_event 가 강제 raise 해도 webhook 정상 응답.
- `tests/test_conversation_log/test_langfuse_xref.py` — NEW. Langfuse active 시 `langfuse_trace` 컬럼이 NOT NULL.
- `tests/test_conversation_log/test_implicit_fb_coexist.py` — NEW. 카드 1개 노출 시 `ai.card_impression` 1 row + `ai.log_conversation_event` (`card_sent`) 1 row 모두 존재.
- `tests/test_conversation_log/test_privacy_delete.py` — NEW. `DELETE FROM ai.log_conversation_event WHERE user_key=$1` 가 한 사용자만 지움.
- `tests/test_conversation_log/test_gin_index.py` — NEW. `EXPLAIN (FORMAT JSON) SELECT … WHERE payload @> '{"intent":"new_search_request"}'` 가 GIN index scan 을 쓰는지.
- `tests/test_conversation_log/test_migration.py` — NEW. `alembic upgrade head` 가 4 인덱스 모두 생성하고 `alembic downgrade -1` 가 클린 drop.

**Reused, untouched modules**:

- `app/providers/db_pool.py` — SPEC-MEMORY-001 의 풀 그대로 재사용. 새 풀 없음.
- `app/channels/session.py` / `taste_profile.py` — Protocol / dataclass 전부 무변경.
- `app/channels/{factory,adapter,vision,vision_prompt,clarify,clarify_values,lang,link_resolver}.py` — 메신저 어댑터 무관.
- `app/channels/implicit_feedback.py` — SPEC-IMPLICIT-FB-001 의 4개 함수 전부 무변경. (단, `record_click` 호출 직후 호출자가 `card_clicked` 를 emit — 호출자 책임, 모듈 내부 변경 없음.)
- `app/pipeline/**` — 검색 파이프라인 무관.
- `app/graphs/state.py` 외 `routing.py`, `fashion_bot.py` — thread_id / turn_no 외 무변경.
- `app/api/{health,recommend}.py` — 무관 (recommend 는 `/recommend` REST 경로 — 본 SPEC 은 Telegram webhook 만 다룸; recommend 노출의 emit 은 향후 SPEC).

---

## Schema Reference (informative — formalized in REQ-LOG-MIGRATION-001)

### `ai.log_conversation_event`

| Column | Type | Notes |
|---|---|---|
| `id` | `bigserial PRIMARY KEY` | Synthetic PK. timeline 순서 보조 (created_at 동률 시 tiebreaker). |
| `user_key` | `text NOT NULL` | `"u:{from_user_id}"` 또는 `"c:{chat_id}"`. SPEC-MEMORY-001 의 `user_taste_profile.user_key` 와 동일 포맷. GDPR ad-hoc delete 의 키. |
| `chat_id` | `bigint NOT NULL` | Telegram chat id. user_key 가 `c:` 변형일 수도 있어 별도 컬럼으로 보존 (group chat / channel 구분 용도 + JOIN 효율). |
| `thread_id` | `uuid` | nullable (webhook 진입 시 항상 채우지만, 노드 외부에서 emit 되는 일부 이벤트 — 예: `taste_update` from cron — 시 NULL 가능; 본 SPEC 범위에서는 모두 채워질 예정이지만 향후 호환성 위해 nullable). |
| `turn_no` | `integer` | nullable. thread 내 노드 순서. webhook = 0, ingest = 1, … |
| `event_type` | `text NOT NULL` | 카탈로그된 17 종 중 하나. 새 타입 추가는 free-form (schema 변경 불필요). |
| `payload` | `jsonb NOT NULL` | 이벤트별 schema (다음 섹션). `'{}'` 도 허용하지만 의미 있는 모든 이벤트는 1 key 이상. |
| `langfuse_trace` | `text` | nullable. v3 trace_id 문자열. Langfuse 비활성 시 NULL. |
| `latency_ms` | `integer` | nullable. 노드 실행 시간 (선택). 없으면 NULL. |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | 서버 시계. timeline 순서의 1차 기준. |

Indexes:

- `PRIMARY KEY (id)`.
- `INDEX idx_log_conv_user_time ON ai.log_conversation_event (user_key, created_at DESC)` — 사용자별 timeline 스캔, GDPR delete 의 표적.
- `INDEX idx_log_conv_thread ON ai.log_conversation_event (thread_id, turn_no)` — turn replay (디버그 사용처).
- `INDEX idx_log_conv_event_type ON ai.log_conversation_event (event_type, created_at DESC)` — type-wise aggregate (analytics 사용처).
- `INDEX idx_log_conv_payload_gin ON ai.log_conversation_event USING GIN (payload)` — `payload @> '{"key":"value"}'` containment 쿼리 (ML 추출 및 ad-hoc 분석).

Foreign keys: **없음** (R3 — `user_session` row 가 TTL 로 사라져도 본 테이블 row 는 영원히 남아야 함; cascade 금지). 같은 user_key 가 `user_taste_profile` 에 존재하지 않을 수도 있음 (e.g., 매우 짧은 첫 세션이 taste_profile 갱신 전에 끝남) — FK 강제 시 데이터 손실 우려.

JSONB 폴리시:

- 모든 nested object / list 는 JSONB. psycopg3 자동 변환 (SPEC-MEMORY-001 와 동일 패턴).
- payload 의 schema 는 docstring + 본 SPEC 의 다음 섹션에서만 정의 — DB-level CHECK constraint 강제 안 함 (스키마 드리프트 시 부담 없이 진화).
- payload version 필드는 향후 schema 진화 시 추가 가능하도록 `payload.v: int` 자리를 권장 (REQ-LOG-PAYLOAD-RICH-001 의 acceptance — 단, 본 SPEC 의 17 이벤트 타입은 모두 `v=1` 묵시).

---

## Event Type Catalog (formalized in REQ-LOG-CATALOG-001)

각 이벤트의 payload 는 TypedDict-style 로 정의 (Python `TypedDict` 와 1:1 매핑). 모든 필드는 명시 안 하면 required. `?` 접미사는 optional. JSON encoding 은 `_to_jsonable` cascade (SPEC-MEMORY-001 의 패턴 재사용).

### 1. `user_text` — 사용자 인바운드 텍스트

```
payload = {
  text: str,                  # raw, 트리밍 안 함
  lang_detected: str,         # "ko" | "en"  (app/channels/lang.py::detect_lang 결과)
}
```

Emitted by: `app/api/webhooks/telegram.py` 인테이크. turn_no=0.

### 2. `user_photo` — 사용자 인바운드 사진

```
payload = {
  attachment_id: str,         # Telegram file_id (Telegram 내부 ref)
  image_url: str | None,      # R2 expiring URL (이미 다운로드된 경우) 또는 Telegram getFile URL
  caption: str | None,        # 사진과 동봉된 캡션
}
```

Emitted by: `app/api/webhooks/telegram.py` 인테이크. turn_no=0.

### 3. `user_callback` — 인라인 버튼 탭

```
payload = {
  callback_data: str,         # raw, "crit:more:0" / "clarify:formality:casual" / "crit:click:abc" / "onboard:..." 등
  source_message_id: int,     # 카드가 들어 있던 메시지 ID
}
```

Emitted by: `app/api/webhooks/telegram.py` 인테이크. turn_no=0.

### 4. `intent_routed` — Router 결정

```
payload = {
  intent: str,                # "new_search_request" | "continue_critique" | "clarify_reply" | "free_text_taste" | "stale_critique" | "noop"
  critique_delta_summary: str | None,  # 직전 critique 가 살아있다면 한줄 요약
}
```

Emitted by: `ingest` 노드. turn_no=1.

### 5. `link_resolved` — Pinterest/pin.it/URL og:image 해석

```
payload = {
  input_url: str,             # 사용자가 보낸 raw URL
  resolved_image_url: str | None,  # og:image 추출 후 R2 / 원본 host URL; 실패 시 None
  host: str,                  # "pinterest.com" | "pin.it" | "other"
}
```

Emitted by: `resolve_image` 노드. turn_no=2.

### 6. `vision_done` — Vision 분석 결과 (SPEC-VISION-UNIFY-001 v2 schema)

```
payload = {
  style_node_primary: str | None,
  style_node_secondary: str | None,
  sensitivity_tags: list[str],
  mood: list[str],
  palette: list[str],
  style: str | None,
  gender: str | None,
  items: list[{
    subcategory: str,
    fit: str | None,
    color_family: str | None,
    search_query: str,
    search_query_ko: str | None,
  }],
  schema_v2_used: bool,       # 흔히 True; legacy minimal schema 폴백 시 False
  error: str | None,          # LLM 호출 자체가 실패해 schema 가 의미 없는 경우
}
```

Emitted by: `vision` 노드. turn_no=3.

### 7. `pick_item_done` — 다중 아이템 disambiguation 결과

```
payload = {
  candidate_items: list[{    # 사용자에게 보여진 후보 목록
    subcategory: str,
    search_query: str,
  }],
  picked_index: int,         # 사용자가 고른 인덱스 (또는 자동 선택 시 0)
  auto_picked: bool,         # 후보 1개라 LLM 호출 없이 자동 선택했는지
}
```

Emitted by: `pick_item` 노드. turn_no=4 (등장 시).

### 8. `ask_clarify_sent` — Clarify 카드 노출

```
payload = {
  axis: str,                  # "category_pick" | "formality" | "fit" | "occasion" | "subcategory_disambiguation" | "generic_fallback"
  options_shown: list[str],   # 표시된 옵션 값 리스트
}
```

Emitted by: `ask_clarify` 노드. turn_no=5 (등장 시).

### 9. `clarify_applied` — Clarify 콜백 소비

```
payload = {
  axis: str,                  # 위와 동일
  value: str,                 # 사용자가 선택한 값
  boost_keywords_added: list[str],  # session.boost_keywords 누적된 항목
}
```

Emitted by: `apply_clarify` 노드. turn_no=동적 (콜백 진입 시 1).

### 10. `search_done` — Search RPC 결과 (ML 핵심)

```
payload = {
  query: {                    # search_products_v5 RPC 의 입력 그대로
    text_query: str | None,
    sparse_terms: list[str] | None,
    embedding_present: bool,  # 임베딩 입력 있었는지 (vector 자체는 너무 커서 미저장)
    filters: dict,            # subcategory / style_node / formality / gender / price 범위 등 그대로
  },
  embedding_ref: str | None,  # 가능하다면 hash (e.g., sha256-prefix-16); 없으면 None
  top_k_product_ids: list[str],  # RPC 가 반환한 product_id 들 (다이버시티 캡 전 — raw 상위)
  rrf_scores: list[float],    # top_k_product_ids 와 같은 길이, 같은 순서
  dense_count: int,           # dense 단계 후보 수
  sparse_count: int,          # sparse 단계 후보 수
  filter_drop_log: list[{     # 필터로 떨어진 product 의 사유 요약 (디버그 replay 핵심)
    product_id: str,
    reason: str,              # "subcategory_mismatch" | "price_out_of_range" 등
  }],
}
```

Emitted by: `search` 노드. turn_no=6 (등장 시).

### 11. `evaluator_run` — Critique loop iteration (SPEC-AGENTIC-CRITIQUE-001)

```
payload = {
  iteration_no: int,          # 0-based; max 2
  score: float,               # evaluator 가 매긴 점수 [0.0, 1.0]
  delta: {                    # 다음 시도용 CritiqueDelta — search 변경 사항
    drop_filters: bool,
    add_keywords: list[str],
    remove_keywords: list[str],
    reason: str,
  } | None,
  retry_decision: str,        # "retry" | "accept" | "exhausted" | "fastpath_drop_filters"
  exhausted: bool,            # max iteration 도달 여부
}
```

Emitted by: `evaluator` 노드, **iteration 당 1회**. turn_no=7+ (반복).

### 12. `diversify_done` — Diversify 결과

```
payload = {
  input_count: int,           # 다이버시티 캡 진입 시 후보 수
  output_count: int,          # 최종 후보 수 (≤ _MAX_CARDS=5)
  brand_cap: int,             # 적용된 브랜드별 캡
  platform_cap: int,          # 적용된 플랫폼별 캡
}
```

Emitted by: 다이버시티 단계 (현재 `send_results` 노드 안 또는 그 직전). turn_no=8 (등장 시).

### 13. `card_sent` — 카드 발송 (timeline view, SPEC-IMPLICIT-FB-001 의 `card_impression` 와 의도된 중복)

```
payload = {
  product_id: str,
  position: int,              # 0-indexed (보통 0~4)
  send_ok: bool,              # Telegram sendPhoto 성공 여부
  send_elapsed_ms: int,       # 단일 카드 sendPhoto 소요 시간
}
```

Emitted by: `send_results` 노드, **카드당 1회**. turn_no=9 (반복).

### 14. `card_clicked` — 카드 클릭 (timeline view, SPEC-IMPLICIT-FB-001 의 `click_status='clicked'` 와 의도된 중복)

```
payload = {
  product_id: str,
  position: int | None,       # source_message_id 로 매칭 가능하면 채움
  dwell_ms: int | None,       # 노출 후 클릭까지 ms (계산 가능 시; card_impression 의 shown_at 기준)
}
```

Emitted by: `critique_apply` 노드의 `crit:click:*` 분기. turn_no=1 (콜백 진입).

### 15. `onboard_select` — 온보딩 카드 다중 선택 (SPEC-ONBOARD-CARDS-001 의존)

```
payload = {
  stage: str,                 # "step_1_style" | "step_2_brand" 등 — SPEC-ONBOARD-CARDS-001 에서 정의
  axis: str,                  # "style" | "brand" | "occasion" 등
  selected_values: list[str], # 사용자가 다중 선택한 값들
}
```

Emitted by: SPEC-ONBOARD-CARDS-001 의 온보딩 노드. SPEC-ONBOARD-CARDS-001 가 land 한 후 활성화 (그 전까지 발화 0).

### 16. `pinterest_ingest` — Pinterest 일괄 처리

```
payload = {
  mode: str,                  # "board" | "profile" | "pin"
  pin_count: int,             # 처리한 pin 수
  vision_results_count: int,  # vision 통과한 결과 수
}
```

Emitted by: `resolve_image` 노드 (Pinterest 분기). turn_no=2.

### 17. `bot_text` — 봇 자연어 응답 chunk

```
payload = {
  chunk_text: str,            # raw 응답 chunk
  chunk_index: int,           # 0-based
  total_chunks: int,          # 같은 turn 내 총 chunk 수
  flow: str,                  # respond.py 의 _Flow enum 값 — "GREETING" | "SEARCH_INTRO" | "NO_RESULTS" 등
}
```

Emitted by: `respond` 노드, **chunk 당 1회**. turn_no=10 (반복).

### 18. `taste_update` — TasteProfile 갱신 이벤트

```
payload = {
  source: str,                # "click" | "onboard" | "pinterest" | "critique" | "free_text" | "no_click" | "re_query"
  keywords_delta: {
    liked_added: list[str],
    disliked_added: list[str],
  },
  brands_delta: {
    liked_added: list[str],
    disliked_added: list[str],
  },
}
```

Emitted by: 모든 TasteProfile 갱신 site — `critique_apply` (click/critique), `taste_update` 노드 (free_text), `implicit_feedback` 모듈 (no_click/re_query). 각 호출자가 source 를 정확히 지정.

### 19. `node_error` — 노드 실행 예외

```
payload = {
  node_name: str,             # 노드 모듈명 — "vision" | "search" 등
  exception_type: str,        # e.g., "TimeoutError" | "ValueError"
  message: str,               # str(exc) — 500자로 truncate
  recovered: bool,            # 그래프가 fallback path 로 계속 진행했는지 / 사용자 에러 메시지로 끝났는지
}
```

Emitted by: 모든 노드의 except 블록. 그래프가 죽지 않을 때만 emit (죽으면 emit 자체가 손실됨 — 받아들임).

### Catalog evolution

새 노드 / 새 이벤트 타입은 본 카탈로그에 추가하면 됨. DB schema 변경 불필요 (event_type 은 free-text). 향후 SPEC 이 새 이벤트 타입을 추가할 때는:

1. 카탈로그에 entry 추가 (이 SPEC 의 후속 PR 또는 새 SPEC).
2. payload 의 첫 키로 `v: int` (현 1) 를 명시 — schema evolve 시 v 를 올려서 분기.
3. 분석 SQL 작성자가 새 타입을 자연스럽게 발견할 수 있도록 `idx_log_conv_event_type` 가 이미 인덱싱.

---

## Requirements & Acceptance Criteria

### REQ Index

| REQ-ID | Title | Priority |
|---|---|---|
| REQ-LOG-MIGRATION-001 | Alembic revision creates `ai.log_conversation_event` with 4 indexes | P0 |
| REQ-LOG-CATALOG-001 | 19 event types enumerated with documented payload schemas | P0 |
| REQ-LOG-THREAD-001 | `thread_id` seeded at webhook intake and propagated through all 12 nodes | P0 |
| REQ-LOG-TURN-001 | `turn_no` monotonically increases (or stays equal) within a thread | P0 |
| REQ-LOG-EMIT-EVERY-NODE-001 | Every graph node emits exactly one terminal event (success or error) per execution | P0 |
| REQ-LOG-FAILSOFT-001 | log_event raises NEVER block the graph; fallback to stderr structured log line | P0 |
| REQ-LOG-FIRE-AND-FORGET-001 | log_event invocations wrapped in `asyncio.create_task` by callers | P0 |
| REQ-LOG-LANGFUSE-XREF-001 | When Langfuse v3 is active, every row includes the current trace_id | P0 |
| REQ-LOG-IMPLICIT-FB-COEXIST-001 | `card_sent` / `card_clicked` coexist with `ai.card_impression` (intentional duplication) | P0 |
| REQ-LOG-PAYLOAD-RICH-001 | `search_done` payload contains parallel `top_k_product_ids[]` and `rrf_scores[]`; arrays equal length | P0 |
| REQ-LOG-PRIVACY-001 | Per-user-key DELETE supported; one user's deletion does not affect others | P0 |
| REQ-LOG-RETENTION-001 | NO automatic deletion / cron / TTL; rows live forever | P0 |
| REQ-LOG-FALLBACK-001 | `memory_backend=in_memory` → all emits silently skip with DEBUG log only | P0 |

---

### Migration (REQ-LOG-MIGRATION-*)

#### REQ-LOG-MIGRATION-001 — Alembic revision SHALL create `ai.log_conversation_event` with the documented 4 indexes [P0]

**THE SYSTEM SHALL** introduce a new Alembic revision file `migrations/versions/0004_create_log_conversation_event.py` (exact filename and `down_revision` chain confirmed in `plan.md` — the revision MUST chain off whatever the latest existing revision is at implementation time, currently expected to be `0003_*` if SPEC-IMPLICIT-FB-001's `card_impression` revision lands as 0003, or `0002_*` if a placeholder pre-exists). The revision SHALL create the table `ai.log_conversation_event` with the 10 columns documented in the Schema Reference, and all 4 indexes (`idx_log_conv_user_time`, `idx_log_conv_thread`, `idx_log_conv_event_type`, `idx_log_conv_payload_gin`). The `downgrade()` function SHALL `DROP TABLE ai.log_conversation_event` (indexes follow automatically).

**Acceptance**:

- `alembic upgrade head` on a Postgres instance with all prior revisions applied creates the table with all 10 columns at correct types, defaults, nullability, and all 4 indexes (verified via `\d ai.log_conversation_event` snapshot).
- `alembic downgrade -1` cleanly drops the table without affecting `user_session`, `user_taste_profile`, or `card_impression`.
- The DDL SHALL use `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` to be idempotent under re-run (consistent with SPEC-MEMORY-001 / SPEC-IMPLICIT-FB-001 pattern).
- The GIN index SHALL use `USING GIN (payload)` with the default `jsonb_ops` operator class (NOT `jsonb_path_ops`) so that both `@>` and `?` / `?&` / `?|` query patterns are supported.
- The migration SHALL run within ≤ 2 seconds on an empty database (DDL only; GIN index build on empty table is constant time).
- A test asserts no FOREIGN KEY clauses are present on the table (R3 — independence from `user_session`).

---

### Catalog (REQ-LOG-CATALOG-*)

#### REQ-LOG-CATALOG-001 — 19 event types SHALL be enumerated and their payload schemas SHALL be testable [P0]

**THE SYSTEM SHALL** define exactly the 19 event types documented in the Event Type Catalog section. Each event type SHALL have a corresponding TypedDict definition in `app/observability/conversation_log.py` (or a sibling `_event_schemas.py` — `plan.md` decides exact placement) so static-type checkers (mypy / pyright) catch payload-construction errors at call sites.

**Acceptance**:

- A unit test enumerates the 19 event type strings (`user_text`, `user_photo`, …, `node_error`) and asserts each has a corresponding TypedDict export from the conversation_log module. The test fails loudly if a new event type is added to the catalog without a TypedDict.
- For each event type, a unit test constructs a minimal valid payload from the TypedDict and asserts:
  - All required fields are present.
  - `json.dumps(payload, default=str)` succeeds (no non-serializable values).
- A documentation test inspects the conversation_log module docstring and asserts every event type from the catalog is mentioned at least once (sanity check for catalog drift between code and SPEC).
- The catalog is considered open-ended: future SPECs MAY add event types by appending to the catalog + TypedDict file. The DB schema requires zero change for additions (only documented entries).

---

### Thread Propagation (REQ-LOG-THREAD-*, REQ-LOG-TURN-*)

#### REQ-LOG-THREAD-001 — `thread_id` SHALL be seeded at webhook intake and propagated through every node [P0]

**WHEN** `POST /webhooks/telegram` accepts an Update,
**THE SYSTEM SHALL** generate a fresh `thread_id = uuid4()` BEFORE invoking the LangGraph, attach it to the `InputState` Pydantic v2 model, and ensure every subsequent node observes the SAME `thread_id` value in its emitted events.

**Acceptance**:

- An integration test plays a full webhook (text message → ingest → vision → search → evaluator → diversify → send_results → respond) and asserts every row in `ai.log_conversation_event` written by that webhook shares one and only one `thread_id` value.
- A unit test asserts `InputState.thread_id` defaults via `Field(default_factory=uuid4)` — i.e., a webhook that forgets to seed still produces a valid (locally unique) thread.
- A unit test asserts `WorkingState` (and `OutputState` if reachable) carry the same field — propagation in the LangGraph state reducer.
- The webhook entry SHALL emit the FIRST event (`user_text` / `user_photo` / `user_callback`) carrying that `thread_id` and `turn_no=0`. Verified by a test that intercepts the first INSERT.
- Cross-webhook independence: two simultaneous webhooks from different chat_ids SHALL produce 2 distinct `thread_id` values (no collisions). Verified by a concurrency test.

#### REQ-LOG-TURN-001 — `turn_no` SHALL be monotonically non-decreasing within a thread [P0]

**WHEN** any node within a single thread emits a row,
**THE SYSTEM SHALL** ensure the `turn_no` column value is greater than or equal to the maximum `turn_no` of all previously emitted rows in that thread. Same-step nodes (e.g., `evaluator` emitting per-iteration rows; `send_results` emitting per-card rows; `respond` emitting per-chunk rows) MAY share the same `turn_no` — this is monotonic non-decreasing, not strictly increasing.

**Acceptance**:

- An integration test of a 3-iteration critique loop asserts `evaluator_run` rows for iterations 0, 1, 2 all carry the same `turn_no` (e.g., 7), with `payload.iteration_no` distinguishing them. The previous node (`search`, `turn_no=6`) is strictly less, the next node (`diversify`, `turn_no=8`) is strictly greater.
- A unit test asserts that for any thread_id, `SELECT turn_no FROM ai.log_conversation_event WHERE thread_id=$1 ORDER BY id` produces a non-decreasing sequence.
- Per-event `turn_no` mapping is a soft convention (documented in the catalog) — not enforced at DB layer. Drift from the documented values is allowed during refactors as long as monotonicity holds.

---

### Emit Coverage (REQ-LOG-EMIT-EVERY-NODE-*)

#### REQ-LOG-EMIT-EVERY-NODE-001 — Every graph node SHALL emit exactly one terminal event per execution (success → typed event OR failure → `node_error`) [P0]

**WHEN** a LangGraph node finishes execution — whether by normal return or by exception bubble-up,
**THE SYSTEM SHALL** ensure at least one row is appended to `ai.log_conversation_event` for that node-instance with thread_id and turn_no consistent with REQ-LOG-THREAD-001 / REQ-LOG-TURN-001:

- Normal return → emit the node's primary event type per the catalog (e.g., `vision` → `vision_done`, `search` → `search_done`).
- Node body raises → the node's except handler emits `node_error` with `payload.recovered` set to `True` (if the graph continues via fallback path) or `False` (if the graph terminates with a user-visible error).

Nodes that emit MULTIPLE rows per execution (e.g., `evaluator` emits 1 per iteration; `send_results` emits 1 per card; `respond` emits 1 per chunk) are still compliant — the "at least one" floor is satisfied.

**Acceptance**:

- An integration test of a happy-path turn (text → vision → search → 3 cards → respond) asserts ≥ 8 rows are produced (1 user_text + 1 intent_routed + 1 vision_done + 1 search_done + 1 diversify_done + 3 card_sent + ≥1 bot_text = ≥ 8). A query `SELECT count(*) WHERE thread_id=$1` returns ≥ 8.
- For each of the 12 nodes, a unit test forces an exception inside the node body and asserts:
  - A `node_error` row is appended with `payload.node_name` matching the failing node.
  - `payload.exception_type` matches the raised class name.
  - `payload.message` is non-empty and truncated to at most 500 characters.
  - The graph either continues (recovered=True) or returns a fallback to the user (recovered=False) — never crashes.
- A code-review-level test (parametric) iterates the 12 node modules and asserts each contains at least one call to `log_event(…, event_type=<expected>, …)`. Achieved by importing each node and inspecting the source for the call site (`ast`-based test) — exact assertion logic is in `plan.md`.
- An integration test of a 100-turn synthetic load (sequential, single-worker) asserts `SELECT count(*) FROM ai.log_conversation_event WHERE created_at > now() - interval '5 minutes'` returns ≥ 800 rows (a floor of 8 events per turn). This is a coverage smoke test — actual count is typically higher.

---

### Fail-Soft (REQ-LOG-FAILSOFT-*, REQ-LOG-FIRE-AND-FORGET-*)

#### REQ-LOG-FAILSOFT-001 — `log_event` SHALL NEVER raise; pool failure SHALL fall back to a stderr structured log line [P0]

**WHEN** `log_event(...)` is invoked,
**THE SYSTEM SHALL** treat the write as best-effort:

1. If `MEMORY_BACKEND_IS_POSTGRES=False` (in-memory fallback per SPEC-MEMORY-001): emit ONE DEBUG-level log line `[CONV_LOG][skip] backend=in_memory event_type=...` and return immediately. No INSERT attempted. No exception.
2. If `MEMORY_BACKEND_IS_POSTGRES=True` and INSERT succeeds: return silently.
3. If the pool acquisition fails (timeout, exhausted) OR the INSERT itself raises (psycopg.OperationalError, schema mismatch, NOT NULL violation, etc.): catch ALL exceptions (`except Exception`), emit ONE WARN-level log line `[CONV_LOG][warn] event_type=... exception_type=... message=...`, AND emit ONE structured stderr log line `[CONV_LOG][stderr_fallback] {full payload as single-line JSON, with thread_id, turn_no, event_type, payload, langfuse_trace, latency_ms, created_at=now()}` so no data is silently lost. Then return.

The function signature is `async def log_event(...) -> None` — no return value, no raised exception. Callers can wrap in `asyncio.create_task` without `.add_done_callback(...)` worry.

**Acceptance**:

- A unit test patches the pool's `connection()` to raise `psycopg.OperationalError` and asserts `await log_event(...)` returns `None` without raising, emits exactly one WARN log line, and emits exactly one stderr fallback line (captured via `capfd` or pytest's `caplog`).
- A unit test forces the INSERT itself to raise (e.g., violating NOT NULL by passing `event_type=None` — type-system would prevent this, but a raw bytes payload achieves the same) and asserts identical behavior — `None` returned, one WARN line, one stderr fallback.
- A property-style test invokes `log_event` 1000 times concurrently with various malformed payloads and asserts: zero exceptions reach the caller, the count of stderr fallback lines plus successful inserts equals 1000 (no silent data loss).
- The stderr fallback line SHALL be valid JSON (parseable by `json.loads`). Verified by a test that captures stderr and feeds each `[CONV_LOG][stderr_fallback]` prefixed line into `json.loads`.
- The stderr fallback semantics intentionally trade write durability (stderr is the container's log driver, which may rotate / lose lines) against write availability (the bot never blocks). The trade-off is documented and accepted.

#### REQ-LOG-FIRE-AND-FORGET-001 — All caller-side emit sites SHALL wrap `log_event` in `asyncio.create_task` [P0]

**THE SYSTEM SHALL** invoke `log_event` only via `asyncio.create_task(log_event(...))` at all call sites in:

- `app/api/webhooks/telegram.py`
- All 12 files under `app/graphs/nodes/`
- `app/channels/implicit_feedback.py` (if a new emit site is added there for `taste_update`)

The task SHALL NOT be awaited. The caller SHALL NOT attach a `.add_done_callback` that re-raises — log_event already handles its own errors per REQ-LOG-FAILSOFT-001.

**Acceptance**:

- An AST-level test parses each call site and asserts the `log_event` invocation appears inside an `asyncio.create_task(...)` (or an equivalent helper). Bare `await log_event(...)` is flagged as a violation.
- A latency test: a node whose body takes 100ms of synthetic work AND emits one `log_event` (whose DB INSERT is patched to take 200ms) SHALL complete its node body in ≤ ~110ms (close to body-only time, not body + DB). The task continues in the background.
- A test SHALL verify that on graph completion, any in-flight log_event tasks are NOT explicitly awaited by the caller (no `.join()` semantic) — the trade-off is that if the container is SIGKILLed mid-task, the row is lost. This is intentional (R5).
- The `asyncio.create_task` wrapping pattern SHALL be encoded in a single helper `emit(event_type, payload, ...)` exported by the conversation_log module so call sites don't repeat the boilerplate. `plan.md` decides the exact helper signature.

---

### Langfuse Cross-Reference (REQ-LOG-LANGFUSE-XREF-*)

#### REQ-LOG-LANGFUSE-XREF-001 — `langfuse_trace` SHALL be populated whenever Langfuse v3 is active [P0]

**WHEN** Langfuse v3 (per SPEC-OBSERVABILITY-002) is active (i.e., `LANGFUSE_PUBLIC_KEY` is set and the client is initialized),
**THE SYSTEM SHALL** populate the `langfuse_trace` column of each emitted row with the current Langfuse trace_id (string). The retrieval SHALL be O(1) via a `current_langfuse_trace_id()` helper that reads the v3 client's context-local trace state — no Langfuse round-trip required.

**WHEN** Langfuse is in no-op fallback (per SPEC-OBSERVABILITY-002),
**THE SYSTEM SHALL** populate the column with `NULL`.

**Acceptance**:

- An integration test with `LANGFUSE_PUBLIC_KEY` set and a mock Langfuse v3 client that returns a fixed trace_id `"trace-abc-123"` asserts every row written during a full turn carries `langfuse_trace='trace-abc-123'`.
- An integration test with Langfuse env vars unset (no-op fallback) asserts every row carries `langfuse_trace IS NULL`.
- The helper `current_langfuse_trace_id()` SHALL NOT raise even if Langfuse internal state is malformed — it returns `None` in any error path. Verified by a unit test that patches the v3 client to raise.
- The `langfuse_trace` value is best-effort: a turn that crosses an `asyncio.create_task` boundary may lose the trace context (Langfuse v3 uses contextvars). The fallback to NULL is acceptable for those tasks. Documented in R8.

---

### Coexistence with `card_impression` (REQ-LOG-IMPLICIT-FB-COEXIST-*)

#### REQ-LOG-IMPLICIT-FB-COEXIST-001 — `card_sent` / `card_clicked` events SHALL be emitted IN ADDITION to `ai.card_impression` writes; both records SHALL exist for the same physical event [P0]

**WHEN** `send_results` dispatches a card to the user,
**THE SYSTEM SHALL** both (a) INSERT into `ai.card_impression` per SPEC-IMPLICIT-FB-001 REQ-FB-IMPRESSION-001 AND (b) emit one `card_sent` event into `ai.log_conversation_event`. The two writes are independent — neither blocks the other, and a failure of one does not prevent the other.

**WHEN** the user taps "👀 자세히" (`crit:click:*` callback per SPEC-IMPLICIT-FB-001 REQ-FB-CLICK-001),
**THE SYSTEM SHALL** both (a) UPDATE `ai.card_impression.click_status` per SPEC-IMPLICIT-FB-001 AND (b) emit one `card_clicked` event into `ai.log_conversation_event`.

**Acceptance**:

- An integration test sends a 3-card carousel and asserts: 3 rows in `ai.card_impression` (with `click_status IS NULL`) AND 3 rows in `ai.log_conversation_event` (with `event_type='card_sent'`) — six rows total, both writes succeeded.
- An integration test taps `crit:click:abc123` and asserts: 1 row in `ai.card_impression` transitions to `click_status='clicked'` AND 1 row in `ai.log_conversation_event` is added with `event_type='card_clicked'` and `payload.product_id='abc123'` — `card_impression` row count stays 3, `log_conversation_event` count goes from 3 → 4.
- An integration test asserts that a `card_clicked` row's `payload.product_id` appears in some prior `card_sent` row within the same `thread_id` (referential correctness — soft, app-level, not DB-enforced).
- A test asserts that a forced failure of the `card_impression` INSERT (e.g., simulated unique violation) does NOT prevent the `card_sent` emit, and vice versa. Both writes are independent.
- The duplication is documented as intentional: `card_impression` is the attribution truth (mutable click_status), `card_sent` / `card_clicked` are the timeline truth (append-only). Future analytics SHALL prefer the timeline view; future reinforcement SHALL prefer the attribution view.

---

### Payload Richness (REQ-LOG-PAYLOAD-RICH-*)

#### REQ-LOG-PAYLOAD-RICH-001 — `search_done` payload SHALL carry parallel `top_k_product_ids[]` and `rrf_scores[]` of equal length [P0]

**WHEN** the `search` node emits `search_done`,
**THE SYSTEM SHALL** populate `payload.top_k_product_ids` (list of strings) AND `payload.rrf_scores` (list of floats) such that:

1. Both arrays have the SAME length (`len(top_k_product_ids) == len(rrf_scores)`).
2. The i-th element of `rrf_scores` corresponds to the i-th element of `top_k_product_ids` (parallel ordering by RRF rank, descending score).
3. Both arrays are populated from the raw `search_products_v5` RPC response BEFORE diversity capping — so they reflect the search engine's view, not the post-diversification view (which `diversify_done` already captures).
4. The arrays MAY be empty (`[]`) if the RPC returned no candidates — in that case both arrays are length 0.

The remaining `search_done` payload keys (`query`, `embedding_ref`, `dense_count`, `sparse_count`, `filter_drop_log`) are documented in the catalog and tested in REQ-LOG-CATALOG-001.

**Acceptance**:

- A unit test invokes the search node with a mocked RPC returning 50 candidates and asserts `payload.top_k_product_ids` and `payload.rrf_scores` are both length 50, with `rrf_scores` monotonically non-increasing.
- A unit test invokes with an empty RPC response and asserts both arrays are `[]` (empty, not `None`, not missing).
- A unit test forces the RPC to return mismatched lengths (e.g., 5 ids but 3 scores — defensive impossible-case) and asserts the emit code raises an internal assertion BEFORE writing to the log, surfaces a `node_error` event with `payload.exception_type='AssertionError'`, and lets the graph continue with empty results. This prevents corrupted ML datasets at the source.
- A retrieval test: after writing one `search_done` row, run `SELECT payload->'top_k_product_ids' AS ids, payload->'rrf_scores' AS scores FROM ai.log_conversation_event WHERE event_type='search_done' LIMIT 1` and assert both JSON arrays have equal length.
- Acceptance for related rich payloads: `vision_done` (REQ-LOG-CATALOG-001 covers the v2 schema fields), `evaluator_run` (iteration_no + score + delta + retry_decision present), `taste_update` (source + keywords_delta + brands_delta present). These are individually covered by REQ-LOG-CATALOG-001 acceptance criteria.

---

### Privacy & Retention (REQ-LOG-PRIVACY-*, REQ-LOG-RETENTION-*)

#### REQ-LOG-PRIVACY-001 — Per-user-key DELETE SHALL be supported and SHALL not affect other users [P0]

**THE SYSTEM SHALL** support GDPR-style user deletion via a single SQL statement:

```sql
DELETE FROM ai.log_conversation_event WHERE user_key = $1;
```

The `idx_log_conv_user_time` index SHALL make this an indexed delete with `O(log N + k)` cost where k is the user's row count.

This SPEC does NOT introduce a REST endpoint or operator CLI for deletion. The DELETE is an operator-driven ad-hoc operation (run via psql or a future operator SPEC). The capability is the requirement; the UX is out of scope.

**Acceptance**:

- An integration test seeds 10 rows for `user_key='u:99'` and 10 rows for `user_key='u:42'`. Runs `DELETE FROM ai.log_conversation_event WHERE user_key='u:99'`. Asserts: 10 rows deleted, `SELECT count(*) WHERE user_key='u:99'` is 0, `SELECT count(*) WHERE user_key='u:42'` is still 10.
- An EXPLAIN test asserts the DELETE uses `idx_log_conv_user_time` (index scan, not seq scan).
- A documentation test asserts the SPEC `plan.md` (when drafted) includes a runbook snippet for the operator delete command (so the capability is discoverable for an oncall responding to a privacy request).
- No PII redaction is applied at write time (raw text / URLs preserved per policy decision B). The deletion path is the only "forget me" mechanism.

#### REQ-LOG-RETENTION-001 — NO automatic deletion, TTL, or cold-storage tier — rows live forever [P0]

**THE SYSTEM SHALL NOT** introduce any cron, background task, scheduled job, or cleanup loop that deletes rows from `ai.log_conversation_event`. The existing `SESSION_CLEANUP_INTERVAL_S` task in `app/channels/session_pg.py` (per SPEC-MEMORY-001 REQ-MEMORY-SESSION-002 and SPEC-IMPLICIT-FB-001 REQ-FB-CLEANUP-001) SHALL NOT be extended to touch `ai.log_conversation_event` in any way.

**Acceptance**:

- A code-review-level test parses `app/channels/session_pg.py` AST and asserts no statement contains the string `log_conversation_event` (lower- or mixed-case). Negative coverage.
- A retention test seeds 1 row, advances simulated time by 365 days (no time-machine — just inspect the cleanup task signatures), and asserts no code path mutates the seeded row.
- The operator runbook documented in `plan.md` SHALL explicitly say: "There is no automatic cleanup. If the table grows beyond comfort, a future SPEC will introduce partitioning or cold storage. Until then, manual archival is the only escape valve."
- R3 explicitly captures the storage-growth risk for review.

---

### Fallback (REQ-LOG-FALLBACK-*)

#### REQ-LOG-FALLBACK-001 — `memory_backend=in_memory` SHALL silently skip all emits with DEBUG log only [P0]

**WHEN** the active memory backend is `in_memory` (per SPEC-MEMORY-001 REQ-MEMORY-HEALTH-001 — bot is in degraded fallback mode),
**THE SYSTEM SHALL** treat every `log_event` invocation as a no-op:

1. Return immediately (`return` after a DEBUG log line) — no INSERT attempted.
2. Emit ONE DEBUG-level log per call: `[CONV_LOG][skip] backend=in_memory event_type=...`. No WARN/ERROR (would flood logs in degraded mode).
3. Callers that wrap in `asyncio.create_task` see the task complete instantly with no exception.

The detection of "memory backend = postgres vs in_memory" SHALL be O(1) — a module-level cached flag set during lifespan startup (sharing the same flag SPEC-IMPLICIT-FB-001 REQ-FB-FALLBACK-001 uses), NOT a per-call `isinstance()` check.

**Acceptance**:

- A unit test sets the module-level flag to in_memory mode, invokes `log_event` 100 times with various payloads, and asserts:
  - No exception raised.
  - Zero rows in `ai.log_conversation_event` (test against testcontainers Postgres — no inserts happened).
  - DEBUG-level log lines captured equal 100.
  - Zero WARN or higher log lines.
- A unit test in postgres-active mode invokes `log_event` and asserts a row IS inserted (positive control).
- The user-visible bot behavior in degraded mode SHALL be byte-identical to the bot before SPEC-CONVERSATION-LOG-001 landed — no extra latency, no missing cards, no new log noise.
- The bot MUST NOT attempt to recover the postgres backend mid-session; fallback is one-way until restart (consistent with SPEC-MEMORY-001 Non-Goal #8 and SPEC-IMPLICIT-FB-001 REQ-FB-FALLBACK-001).
- An end-to-end test starts the app with unreachable `DB_DSN` and `MEMORY_FALLBACK_ON_PROBE_FAIL=true`, sends 10 webhooks, asserts: bot responds normally to all 10, no rows in `ai.log_conversation_event` (we have no DB to write to), DEBUG-level skip lines visible in stdout.

---

## Environment Variables (introduced by this SPEC)

**None.**

본 SPEC 은 새 env var 를 도입하지 않는다. 모든 wiring 은 기존 `MEMORY_BACKEND_IS_POSTGRES` flag (SPEC-MEMORY-001 / SPEC-IMPLICIT-FB-001 공용), 기존 `LANGFUSE_PUBLIC_KEY` (SPEC-OBSERVABILITY-002), 기존 `DB_DSN` / 풀 (SPEC-MEMORY-001) 만 사용한다.

향후 운영 중 nuanced 제어가 필요하면 (예: 특정 event_type 만 disable, 또는 row 압축 임계 — 둘 다 NON-GOAL 인 본 SPEC 단계) 후속 SPEC 에서 env var 를 추가한다.

---

## Non-Goals (out of scope for this SPEC)

The following are explicitly NOT delivered by SPEC-CONVERSATION-LOG-001 and MUST NOT be conflated with it:

1. **Per-event-type strongly-typed tables (option B).** 사용자가 option A 를 명시적으로 선택. 17개 sibling table 분리는 향후 데이터 양이 JSONB GIN 으로 못 버틸 때 검토.
2. **Pseudonymization, hashing, or redaction of stored text/URLs.** 사용자가 raw 저장을 명시적으로 선택 (정책 결정 B). 향후 별도 privacy SPEC 이 column-level encryption 도입 가능하지만 본 SPEC 범위 외.
3. **Cold storage tier (S3 / Iceberg archive).** 30일 / 90일 / 365일 분기 archival 없음. 디스크 부담이 실제로 닥칠 때 별도 SPEC.
4. **Cron-based deletion or TTL.** 시간 기반 자동 삭제 없음 (REQ-LOG-RETENTION-001). retention policy 의 진화는 별도 SPEC.
5. **Online analytics dashboard / Metabase / Grafana connector.** SQL 분석은 psql 직접 또는 ad-hoc 쿼리로. 시각화 도구 연결은 별도 SPEC.
6. **Replacing Langfuse.** Langfuse v3 trace tree 는 그대로 30일 보존 + 시각화 용도. 본 SPEC 은 보완(cross-ref) 만 한다.
7. **Real-time streaming consumers (Debezium / Kafka).** logical decoding wire-up 없음. 향후 ML 파이프라인이 필요해지면 SPEC 추가.
8. **Backfill of past sessions.** 본 SPEC 머지 시점부터 데이터 시작. 과거 Telegram chat 히스토리에서 재현하지 않음.
9. **Per-event-type rate limiting or sampling.** 모든 이벤트 100% 캡처. 향후 spike 시 sampling 도입 가능하지만 본 SPEC 범위 외.
10. **Operator-facing REST/CLI for privacy delete.** SQL ad-hoc 만 지원 (REQ-LOG-PRIVACY-001). 사용자 facing "forget me" 엔드포인트는 별도 privacy SPEC.
11. **Encryption at rest of payload column.** dev-app Postgres 의 표준 EBS 암호화에 의존. column-level encryption 없음.
12. **Multi-region replication of the log.** 단일 Postgres 인스턴스. HA / DR 은 인프라 SPEC.
13. **Schema migration of past in-memory or stdout logs.** 이전 운영 데이터는 import 하지 않음.
14. **Cross-worker concurrent emit semantics beyond what `asyncio.create_task` provides.** SPEC-MEMORY-001 Non-Goal #9 의 `--workers 1` 가정 유지. 다중 워커 환경에서의 thread_id 충돌은 (uuid4 충돌 확률 0 이지만) 본 SPEC 의 책임 아님.
15. **Changes to `Session`, `TasteProfile`, `card_impression` data models.** Protocol / dataclass / 테이블 모두 무변경.
16. **Adding new graph nodes.** 12 노드 그대로. 본 SPEC 은 노드 *내부* 의 emit 호출만 추가.
17. **Modifying the search pipeline scoring or ranking.** `search` 노드는 결과를 *기록만* 한다.
18. **Modifying the critique loop iteration count or termination logic.** SPEC-AGENTIC-CRITIQUE-001 의 max=2 등 invariant 그대로.
19. **Modifying the implicit feedback weights or attribution windows.** SPEC-IMPLICIT-FB-001 의 모든 env var 그대로.
20. **PostgREST exposure of the new table.** internal 메타데이터 — nginx PostgREST shim 비공개.
21. **A `CONV_LOG_ENABLED=false` master kill switch.** 의도적 omit — degraded mode 가 자동 fallback path (REQ-LOG-FALLBACK-001) 로 emit 을 0 으로 만드므로 별도 flag 불필요. 응급 disable 이 정말 필요하면 향후 SPEC.

---

## Exclusions (What NOT to Build)

(Mirrors Non-Goals — explicit list for SPEC-checker compliance.)

1. No per-event-type sibling tables.
2. No PII hashing, redaction, or masking.
3. No cold storage tier or archival pipeline.
4. No cron-based deletion or TTL.
5. No analytics dashboard / Metabase / Grafana wiring.
6. No replacement for Langfuse trace tree.
7. No streaming consumers (Debezium / Kafka).
8. No backfill of past data.
9. No per-event sampling or rate limiting.
10. No REST/CLI for privacy delete (SQL ad-hoc only).
11. No column-level encryption.
12. No multi-region replication.
13. No migration from past stdout logs.
14. No cross-worker concurrency primitives beyond `asyncio.create_task`.
15. No data model changes to existing tables.
16. No new graph nodes.
17. No search pipeline scoring changes.
18. No critique loop logic changes.
19. No implicit feedback weight changes.
20. No PostgREST exposure of the new table.
21. No master kill switch env var.

---

## Stakeholders

| Role | Responsibility |
|---|---|
| Product / Founder (hchsa77@gmail.com) | Confirmed the four policy decisions (single-table option A, raw PII, permanent retention, fire-and-forget) in the round leading to this SPEC. Approves the data-moat positioning of this SPEC as the foundation for future ML re-ranking and behavior-analytics work. |
| AI Server Owner (this SPEC) | All work in `app/observability/conversation_log.py` (NEW), 12 node files (MODIFIED), `app/api/webhooks/telegram.py` (MODIFIED), `app/graphs/state.py` (MODIFIED), `migrations/versions/0004_*.py` (NEW). Owns the 11 test files. Owns runbook in `plan.md` for operator-level privacy delete. |
| dev-app Postgres operator | Provisions DB user with INSERT on `ai.log_conversation_event`. Verifies pool headroom (no new pool — reuses SPEC-MEMORY-001's 10-connection pool). Monitors disk growth (R3) over the first 30 days post-cutover. Acts on any partitioning recommendation if growth exceeds the budget. |
| Langfuse operator | No action required. The `langfuse_trace` column is populated automatically when SPEC-OBSERVABILITY-002's v3 client is active. The Langfuse storage envelope is unaffected (no new spans). |
| Future ML / analytics consumer (out of scope) | Will consume `ai.log_conversation_event` via direct SQL or future ETL. The catalog + payload schemas are the API contract. |
| Modal / kikoai/app teams | Out of scope. The log is internal to kikoai/ai. The web app (`kikoai/app`) does not emit into this table; if web-side implicit feedback becomes a concern, a future SPEC will add a web event source. |

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Write amplification** — Postgres write QPS could increase 10x (~8 events/turn × N concurrent turns). Pool exhaustion or replica lag at scale. | Medium | Medium | Single batched-INSERT pattern is NOT used (per event = 1 INSERT, fire-and-forget). The 10-connection pool (SPEC-MEMORY-001 default) tolerates ~3-5 concurrent turns × 8 events with comfortable headroom. JSONB binary protocol minimizes wire overhead. `idx_log_conv_payload_gin` is lazy-built (no synchronous fanout on insert — GIN index updates are append-only and cheap). For burst scenarios, the `MEMORY_POOL_MAX_SIZE` env can be raised without code change. |
| R2 | **PII in Postgres** — raw user text and image URLs sit unencrypted at row level. A breach or audit query exposes user content. | Medium | High | (a) Row-level DELETE supported by `user_key` index (REQ-LOG-PRIVACY-001) — fast GDPR response. (b) EBS encryption at rest covers physical media. (c) DB user for the app SHALL have minimum-required grants (INSERT only — operator-level access for audit / privacy is separate). (d) Future SPEC may introduce column-level encryption if external audit demands. (e) Documented in policy decision B — user explicitly accepted the raw-storage trade-off. |
| R3 | **Storage growth at scale.** Rough math: ~8 events/turn × 2KB JSONB avg = 16KB/turn. 1000 turns/day × 365 days × 16KB ≈ **5.8GB/year**. At 10K turns/day (10x growth) it's ~58GB/year. Single dev-app EBS volume tolerates this for years, but JSONB GIN index also grows. | Medium | Medium | (a) No partitioning in this SPEC (Non-Goal #4 forbids time-based cron, but partitioning is an orthogonal future SPEC). (b) `idx_log_conv_payload_gin` is the largest index — JSONB containment queries are needed for ML extraction; if size becomes a problem, switch to `jsonb_path_ops` (smaller, narrower query support) or drop GIN and use materialized views (future SPEC). (c) Disk usage alarms at 70% threshold (operator runbook, in `plan.md`). (d) The "permanent retention" policy may be re-evaluated in 12 months — but this re-evaluation is itself a future SPEC, not a TTL cron. |
| R4 | **Schema drift in JSONB payload.** A new field added to `vision_done` payload without catalog update breaks downstream ML extraction. | High | Medium | (a) REQ-LOG-CATALOG-001's TypedDict + parametric tests catch additions at PR time. (b) `payload.v` field convention (current `v=1` implicit) allows future schema evolution with explicit version bump. (c) Each event type's payload SHALL be a TypedDict export — type checker enforces shape at call site. (d) A lint rule could be added in CI to flag `log_event(event_type='X', payload={...})` calls whose payload dict doesn't construct from the typed class (deferred — manual review for now). |
| R5 | **Lost events on container crash mid-task.** `asyncio.create_task` returns a coroutine that runs on the event loop. SIGKILL / OOM-kill ends the loop instantly; in-flight tasks lose their payloads. | Low | Low | Accepted trade-off per policy decision D. The bot's eventually-consistent surfaces (`user_session`, `user_taste_profile`) are written via `update()` which IS synchronous (per SPEC-MEMORY-001) — those snapshots are not corrupted. Only the timeline / replay capability has a small data gap during the seconds before a crash. Stderr fallback (REQ-LOG-FAILSOFT-001) captures pool-failure cases but not container-crash cases — the latter is a Docker log driver responsibility. |
| R6 | **Privacy / legal request flood** — multiple simultaneous "forget me" deletes could saturate the pool. | Low | Low | Per-user-key DELETE is indexed (REQ-LOG-PRIVACY-001) and runs in < 100ms even for 10K rows per user. 10 concurrent privacy deletes consume 10 / 10 pool connections briefly — application traffic is held up for < 1s. If volume becomes a concern, a future SPEC introduces a privacy-delete queue. |
| R7 | **`asyncio.create_task` reference leak** — Python's asyncio strongly warns against creating tasks without retaining a reference (the GC can collect a not-yet-started task). | Low | Medium | The `emit(...)` helper (REQ-LOG-FIRE-AND-FORGET-001 acceptance) SHALL retain task references in a module-level `WeakSet` and drop them on completion (standard Python idiom). Verified by a stress test that emits 10K events and asserts none are lost to GC. |
| R8 | **Langfuse trace_id propagation across `asyncio.create_task` boundaries.** Langfuse v3 uses contextvars; spawning a task may break the binding. | Medium | Low | The `current_langfuse_trace_id()` helper SHALL be called in the *caller's* context BEFORE spawning the task — i.e., the trace_id is captured as a local variable and passed as an argument to `log_event`. The task itself doesn't need contextvar access. Verified by an integration test that confirms `langfuse_trace` is populated for `card_sent` events emitted from `send_results` (which is the busiest emit site). |
| R9 | **Synthetic test load (REQ-LOG-EMIT-EVERY-NODE-001 acceptance) hits the testcontainers Postgres at ~800 inserts in 5 minutes.** CI runtime budget for the test suite. | Medium | Low | The 100-turn sequential test runs in < 30 seconds locally (synthetic webhooks, mocked LLM / RPC). The 800-row floor is generous — actual count is typically ~1200. CI flakiness budget: 20% headroom. |
| R10 | **GIN index size on `payload`** could grow to 2-3x the table size if payloads are large (vision_done's v2 schema). | Medium | Medium | (a) `idx_log_conv_payload_gin` is the only GIN — its growth is concentrated, easy to monitor (`pg_indexes` view). (b) `vision_done` payload is bounded by the Vision LLM's `VISION_MAX_TOKENS` (~1KB JSON typical, ~4KB worst case). Not pathological. (c) If size becomes a concern, swap to `jsonb_path_ops` — narrower operator support (`@>` only) but ~2x smaller. Decision deferred. |
| R11 | **Time-skew between thread_id seed and first node emit.** If webhook intake is delayed (e.g., Telegram retry), the FIRST event's created_at may not be the earliest in the thread. | Low | Low | The webhook intake's three event types (`user_text` / `user_photo` / `user_callback`) are emitted *before* graph invocation, so they always lead. The `id BIGSERIAL` PK provides a guaranteed tiebreaker order independent of created_at. Replay logic SHALL use `(thread_id, id ASC)` for ordering, not `created_at`. |
| R12 | **Adding emit calls to 12 node files increases the diff surface.** Risk of accidental regression in nodes' core logic during edit. | Medium | Medium | Each node modification is a single-purpose change: insert one emit call at the success terminus + wrap body in try/except for `node_error` emit. PR review SHALL surface any logic touch beyond that. Per-node unit tests already exist (per SPEC-AGENT-001 acceptance) — they continue to pass after the emit additions. |
| R13 | **`embedding_ref` in `search_done` payload.** The Modal-returned embedding vector is too large (3072 floats) to store inline. If we store nothing, replay can't reproduce the search; if we store a hash, hash collisions are a non-issue but storage savings are nominal. | Medium | Low | Store `embedding_ref` as `sha256(vector_bytes)[:16]` — 16 hex chars. Sufficient to detect "same embedding submitted twice" without storing the vector. For replay, the embedding must be re-derived from the original image — which is OK because `vision_done` carries the items/keywords (the canonical input shape downstream of Vision). Future SPEC may add a separate `embedding_archive` table if vector replay is critical. |
| R14 | **TypedDict-vs-dict drift.** Python's TypedDict is structural — passing a wrong-key dict is a runtime no-op (no error). Tests must enforce schema. | High | Medium | REQ-LOG-CATALOG-001's per-event-type unit test enforces required-key presence (`assert "text" in payload`). A linter rule (deferred to `plan.md`) could catch dict literal sites that don't construct from the typed class. The pragmatic mitigation is: the 17-event-type test suite is the gate. |
| R15 | **Cross-table consistency between `card_sent` and `card_impression`.** A `card_impression` INSERT succeeds while the parallel `card_sent` emit fails (or vice versa), leaving the two views disagreeing. | Medium | Low | The two writes are independent and the divergence is documented as acceptable (REQ-LOG-IMPLICIT-FB-COEXIST-001). Analytics that needs precise click attribution SHALL use `card_impression` (the SPEC-IMPLICIT-FB-001 source-of-truth). Analytics that needs timeline SHALL use `log_conversation_event` (best-effort). The two are not joined as foreign keys for this reason. |
| R-DUP | **`card_sent` / `card_clicked` row volume duplication.** Per impression, 2 rows persist (one per table). At 5 cards × 30 turns/day × 365 = 54K row-pairs/year. | Low | Low | Acceptable disk overhead (~108KB/year per active user for these two row classes). The duplication unlocks both attribution (mutable single row) and timeline (append-only) use cases without one paying the other's cost. Documented in Background section. |
| R-PII | **payload contains raw user PII (text, URLs, captions, callback_data, vision results that may include nudity/sensitivity tags).** Internal users (founder, oncall) can read the log. | High | Medium | (a) Policy decision B accepts raw PII storage — founder is the data controller. (b) DB-level access is restricted to the app user + operator user (no shared service accounts). (c) future privacy SPEC may add audit logging of operator-level SELECT queries. (d) GDPR delete (REQ-LOG-PRIVACY-001) is the user-facing escape hatch. |

---

## Open Questions (deferred to plan.md / implementation)

본 SPEC 단계에서 의도적으로 deferred. 본 SPEC 승인을 막지 않지만 코드 작성 전 plan.md 에서 결정해야 한다:

1. **Exact `_to_jsonable` cascade reuse vs new module.** SPEC-MEMORY-001 REQ-MEMORY-SESSION-001 의 5-step cascade 를 `app.observability.conversation_log` 가 import 해서 재사용할지, 또는 새 `_payload_to_jsonable` 헬퍼를 박을지. `plan.md` 가 결정 — 코드 중복 vs 모듈 결합도 트레이드오프.
2. **`current_langfuse_trace_id()` 의 정확한 v3 API call.** SPEC-OBSERVABILITY-002 가 land 한 v3 client (`langfuse.Langfuse`) 의 어떤 메서드가 context-local trace_id 를 반환하는지 — v3 docs 의 `get_current_observation().trace_id` 또는 `langfuse_context.get_current_trace_id()` — `plan.md` 가 실측해서 확정. 둘 다 안 되면 langchain `RunnableConfig` 의 callback metadata 를 통해 우회.
3. **Migration 번호.** SPEC-IMPLICIT-FB-001 의 `0002_create_card_impression.py` 가 이미 land 되어 있으므로 본 SPEC 의 revision 은 0003 또는 0004. 현재 `migrations/versions/` 디렉토리 상태(0001, 0002 가 land 됨; 0003 미정)를 plan.md 에서 확인 후 확정.
4. **`emit(...)` helper signature.** REQ-LOG-FIRE-AND-FORGET-001 acceptance 에서 module-level helper 의 시그니처(positional vs kwargs only, default 값, async vs sync wrapper)를 plan.md 에서 결정. 기본 안: `def emit(event_type: str, payload: dict, *, thread_id: UUID, turn_no: int, user_key: str, chat_id: int) -> asyncio.Task`.
5. **Stderr fallback format.** REQ-LOG-FAILSOFT-001 의 stderr line 이 정확히 어떤 JSON 키 집합인지 — full row 직렬화인지 일부 핵심 필드만인지. 운영자가 `grep '\[CONV_LOG\]\[stderr_fallback\]' | jq -s '...'` 로 archive 할 수 있는 포맷이어야 함.
6. **AST-level test for "every node emits".** REQ-LOG-EMIT-EVERY-NODE-001 acceptance 의 parametric test 가 어떻게 노드 모듈을 import 하고 `log_event` 호출을 찾는지의 정확한 패턴. 후보: (a) `ast.parse(src).walk()` 로 `Call(func=Name("log_event"))` 검색, (b) `inspect.getsource(node_module)` regex 검색. plan.md 가 결정.
7. **`embedding_ref` hash 알고리즘.** R13 에서 sha256-prefix-16 으로 제안했지만 plan.md 가 sha1 / blake2 / fxhash 등 더 빠른 옵션을 검토해 확정. embedding 벡터 hashing 은 turn 당 1회라 latency 의 0.1% 미만.
8. **`payload.v` 필드 도입 여부.** R4 mitigation 의 `payload.v: int = 1` 명시는 현재 묵시. plan.md 에서 명시적으로 모든 payload 첫 키로 박을지(`{"v": 1, "text": ...}`), 또는 향후 schema evolution 시 retroactive 도입할지 결정.

---

## Cross-References

- **Builds on (HARD)**:
  - SPEC-MEMORY-001 — `ai` 스키마, Alembic baseline, `psycopg.AsyncConnectionPool` (`app/providers/db_pool.py::get_pool()`), `MEMORY_BACKEND_IS_POSTGRES` flag, fail-soft 패턴 (no-PII span helper, sanitized DSN logging).
  - SPEC-AGENT-001 — 12 노드 토폴로지. 본 SPEC 은 12 노드 모두에 emit 호출을 박는다 (REQ-LOG-EMIT-EVERY-NODE-001).
- **Builds on (SOFT)**:
  - SPEC-IMPLICIT-FB-001 — `ai.card_impression` 와 `card_sent`/`card_clicked` 이벤트의 공존 (REQ-LOG-IMPLICIT-FB-COEXIST-001). SPEC-IMPLICIT-FB-001 의 `crit:click:*` 콜백 분기가 `card_clicked` emit 의 source. 본 SPEC 은 SPEC-IMPLICIT-FB-001 의 기존 attribution 로직을 *건드리지 않는다* — INSERT 한 줄만 추가.
  - SPEC-OBSERVABILITY-002 — Langfuse v3 active 시 `langfuse_trace` 컬럼 채워짐 (REQ-LOG-LANGFUSE-XREF-001). 비활성 (no-op fallback) 시 NULL — 본 SPEC 은 SPEC-OBSERVABILITY-002 없이도 동작.
  - SPEC-ONBOARD-CARDS-001 — 본 SPEC 의 catalog 에 `onboard_select` 이벤트 타입 등재. SPEC-ONBOARD-CARDS-001 가 land 한 후 자동 활성화 (그 전까지는 catalog 의 unused entry).
- **Triggers / unblocks**:
  - Future SPEC: ML re-ranking model training (now has `(query → top_k → click)` sequence as a single SQL extract).
  - Future SPEC: Behavior analytics dashboards (Metabase / Grafana — now has source table).
  - Future SPEC: Cross-user aggregation views (now has stable schema to materialize into).
  - Future SPEC: Multi-turn attribution heuristics (now has full timeline replay).
  - Future SPEC: Privacy-delete REST endpoint (now has indexed per-user-key delete primitive).
  - Future SPEC: Partition `ai.log_conversation_event` by month (when storage growth crosses operator threshold).
  - Future SPEC: Cold storage tier (S3 / Iceberg archive) for rows older than N months.
- **Affected modules in kikoai/ai**:
  - NEW: `app/observability/conversation_log.py`, `migrations/versions/0004_create_log_conversation_event.py` (exact number resolved in `plan.md`), `tests/test_conversation_log/test_log_event.py`, `tests/test_conversation_log/test_thread_propagation.py`, `tests/test_conversation_log/test_payload_shapes.py`, `tests/test_conversation_log/test_search_payload.py`, `tests/test_conversation_log/test_node_error.py`, `tests/test_conversation_log/test_failsoft.py`, `tests/test_conversation_log/test_langfuse_xref.py`, `tests/test_conversation_log/test_implicit_fb_coexist.py`, `tests/test_conversation_log/test_privacy_delete.py`, `tests/test_conversation_log/test_gin_index.py`, `tests/test_conversation_log/test_migration.py`.
  - MODIFIED: `app/graphs/state.py` (add `thread_id` + `turn_no` to InputState/WorkingState), `app/api/webhooks/telegram.py` (seed thread_id + emit inbound events), `app/graphs/nodes/ingest.py`, `app/graphs/nodes/resolve_image.py`, `app/graphs/nodes/vision.py`, `app/graphs/nodes/pick_item.py`, `app/graphs/nodes/ask_clarify.py`, `app/graphs/nodes/apply_clarify.py`, `app/graphs/nodes/search.py`, `app/graphs/nodes/evaluator.py`, `app/graphs/nodes/send_results.py` (per-card emit + diversify_done emit), `app/graphs/nodes/respond.py` (per-chunk emit), `app/graphs/nodes/taste_update.py`, `app/graphs/nodes/critique_apply.py` (card_clicked branch + taste_update emit), `app/observability/langfuse.py` (export `current_langfuse_trace_id()`), `app/main.py` (lifespan ensures conversation_log module reads the shared `MEMORY_BACKEND_IS_POSTGRES` flag).
  - UNCHANGED (asserted): `app/channels/session.py`, `app/channels/taste_profile.py`, `app/channels/implicit_feedback.py`, `app/channels/session_pg.py`, `app/channels/taste_profile_pg.py`, `app/providers/db_pool.py`, `app/providers/database.py`, `app/pipeline/**`, `app/graphs/routing.py`, `app/graphs/fashion_bot.py`, `app/channels/{factory,adapter,vision,vision_prompt,clarify,clarify_values,lang,link_resolver}.py`, `app/channels/telegram/*`, `app/api/{health,recommend}.py`, `app/models/**`.
- **Project context**: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md`.
- **Research basis**: `docs/_tmp/noscroll-benchmark.html` — noscroll 벤치마크 리서치의 "사용자 행동 시퀀스 = 데이터 해자" 관찰 + 본 라운드의 사용자 정책 결정 4종 (option A, raw PII, permanent retention, fire-and-forget) + 세 가지 사용처 (behavior analytics + ML dataset + debug replay).

---

## Definition of Done (P0)

- [ ] REQ-LOG-MIGRATION-001 implemented. Alembic revision creates `ai.log_conversation_event` with 10 columns and 4 indexes (`idx_log_conv_user_time`, `idx_log_conv_thread`, `idx_log_conv_event_type`, `idx_log_conv_payload_gin USING GIN`). `alembic upgrade head` and `alembic downgrade -1` both succeed on dev Postgres. No FOREIGN KEY clauses present.
- [ ] REQ-LOG-CATALOG-001 implemented. 19 event types (user_text, user_photo, user_callback, intent_routed, link_resolved, vision_done, pick_item_done, ask_clarify_sent, clarify_applied, search_done, evaluator_run, diversify_done, card_sent, card_clicked, onboard_select, pinterest_ingest, bot_text, taste_update, node_error) each have a TypedDict export. Per-type payload smoke test passes.
- [ ] REQ-LOG-THREAD-001 implemented. `InputState.thread_id: UUID` + `InputState.turn_no: int` added with sensible defaults. webhook intake seeds a fresh `uuid4()` per request. Full-turn integration test asserts one and only one thread_id per turn.
- [ ] REQ-LOG-TURN-001 implemented. Per-node turn_no follows the catalog's documented values (webhook=0, ingest=1, resolve_image=2, …, respond=10). For nodes that emit multiple rows (evaluator iterations, send_results cards, respond chunks), all rows share the same turn_no. Non-decreasing monotonicity test passes.
- [ ] REQ-LOG-EMIT-EVERY-NODE-001 implemented. AST-level test verifies each of the 12 node files contains at least one `log_event` invocation. Full happy-path turn produces ≥ 8 rows; 100-turn synthetic load produces ≥ 800 rows. Each of the 12 nodes has a forced-exception test asserting `node_error` row is appended.
- [ ] REQ-LOG-FAILSOFT-001 implemented. `log_event` never raises; pool failure produces one WARN log + one stderr structured JSON line; the bot continues normally. 1000-call concurrent property test asserts zero exceptions and zero data loss (insert + stderr counts sum to 1000).
- [ ] REQ-LOG-FIRE-AND-FORGET-001 implemented. All `log_event` invocations wrapped in `asyncio.create_task` via an `emit(...)` helper. Latency test asserts node body completes in body-only time even when the DB INSERT is artificially slow. WeakSet retention prevents task GC loss.
- [ ] REQ-LOG-LANGFUSE-XREF-001 implemented. Langfuse v3 active → every row carries `langfuse_trace = trace_id`. Langfuse no-op → every row carries `langfuse_trace IS NULL`. `current_langfuse_trace_id()` helper never raises. trace_id captured in caller context before `asyncio.create_task` spawn.
- [ ] REQ-LOG-IMPLICIT-FB-COEXIST-001 implemented. 3-card send → 3 rows in `card_impression` AND 3 rows in `log_conversation_event` (event_type='card_sent'). Click on card 2 → row in `card_impression` flips to `clicked` AND new row in `log_conversation_event` (event_type='card_clicked'). Either write failing does not block the other.
- [ ] REQ-LOG-PAYLOAD-RICH-001 implemented. `search_done.payload.top_k_product_ids` and `rrf_scores` are parallel arrays of equal length. Empty-RPC case → both `[]`. Mismatched-length defensive assertion raises `node_error` before write. JSON `->` extraction returns the arrays correctly.
- [ ] REQ-LOG-PRIVACY-001 implemented. `DELETE FROM ai.log_conversation_event WHERE user_key=$1` uses `idx_log_conv_user_time`. Two-user test asserts deletion isolation. Operator runbook documented in `plan.md`.
- [ ] REQ-LOG-RETENTION-001 implemented. AST scan of `app/channels/session_pg.py` confirms no `log_conversation_event` reference. Operator runbook in `plan.md` explicitly states no automatic cleanup.
- [ ] REQ-LOG-FALLBACK-001 implemented. `MEMORY_BACKEND_IS_POSTGRES=False` → 100 calls produce 0 rows + 100 DEBUG lines + 0 WARN lines. 10-webhook end-to-end with unreachable `DB_DSN` → bot responds normally, 0 rows, DEBUG-only logs.
- [ ] All existing tests (`pytest -q` baseline before this SPEC, including SPEC-MEMORY-001 + SPEC-IMPLICIT-FB-001 + SPEC-OBSERVABILITY-002 + SPEC-AGENT-001 suites) continue to pass under both backends. The 12-node tests in particular MUST be re-run after the emit additions to confirm no regression.
- [ ] **Coverage target (TRUST 5 Tested):** `app/observability/conversation_log.py` reports ≥ 85% line coverage. The 11 new test files in `tests/test_conversation_log/` collectively cover every public symbol of the module and every event type in the catalog.
- [ ] `migrations/versions/0004_create_log_conversation_event.py` (or the resolved revision number) exists with `down_revision` correctly chained to the latest prior revision. DDL matches Schema Reference exactly (10 columns, 4 indexes, no FK).
- [ ] An end-to-end manual test against the dev Telegram bot exercises:
      (a) `/start` → `psql -c "SELECT count(*) FROM ai.log_conversation_event WHERE event_type='user_text' AND payload->>'text'='/start'"` returns 1; `payload->>'lang_detected'` is set.
      (b) Photo → full turn → ≥ 8 rows for that thread_id (one per terminal node + one per card); `SELECT event_type, turn_no FROM ai.log_conversation_event WHERE thread_id=$1 ORDER BY id` shows non-decreasing turn_no with `user_photo, intent_routed, vision_done, search_done, diversify_done, card_sent, card_sent, card_sent, bot_text`.
      (c) Tap "👀 자세히" on card 2 → `card_clicked` row appears in `log_conversation_event` with `payload.product_id` matching one of the previously-shown `card_sent` rows' `product_id` (within the same thread_id).
      (d) Force PG pool shutdown mid-turn (`docker pause` postgres) → bot completes the turn, returns cards to the user, WARN log lines emitted, stderr fallback JSON visible in `docker logs`.
      (e) `EXPLAIN (FORMAT JSON) SELECT * FROM ai.log_conversation_event WHERE payload @> '{"intent":"new_search_request"}'` uses `Bitmap Index Scan on idx_log_conv_payload_gin`.
      (f) `DELETE FROM ai.log_conversation_event WHERE user_key='u:99'` (a real test user) removes only that user's rows; verify other users' counts unchanged.
      (g) `SELECT count(*) FROM ai.log_conversation_event WHERE created_at > now() - interval '5 minutes'` after 100 simulated turns returns ≥ 800 rows.
- [ ] `ruff check . && ruff format --check .` passes.
- [ ] `pytest -q` passes at the same or higher count vs the pre-SPEC baseline; new test count includes the 11 test files in `tests/test_conversation_log/` (test_log_event, test_thread_propagation, test_payload_shapes, test_search_payload, test_node_error, test_failsoft, test_langfuse_xref, test_implicit_fb_coexist, test_privacy_delete, test_gin_index, test_migration). Total new test case count formalized in `acceptance.md`.

---

## Implementation Plan Outline (informative — formalized in plan.md)

1. **Alembic revision** (`0004_create_log_conversation_event.py` or resolved number): hand-write DDL + 4 indexes with `IF NOT EXISTS`; `alembic upgrade head` on local dev Postgres.
2. **`conversation_log.py` module**: `log_event` async function with full exception swallow + stderr fallback. `emit(...)` helper that wraps in `asyncio.create_task` and retains in WeakSet. `current_langfuse_trace_id()` proxy. TypedDicts for the 19 event types.
3. **`state.py` modification**: add `thread_id: UUID = Field(default_factory=uuid4)` + `turn_no: int = 0` to InputState + WorkingState.
4. **Webhook intake** (`app/api/webhooks/telegram.py`): seed thread_id + emit `user_text` / `user_photo` / `user_callback` per inbound type.
5. **Node modifications** (12 files): one emit per success terminus + try/except for `node_error` emit. Care taken to NOT change the node's primary logic (per scope discipline).
6. **Langfuse helper export** (`app/observability/langfuse.py`): `current_langfuse_trace_id()` reads the v3 client's context-local state (or returns None on no-op).
7. **Lifespan wiring** (`app/main.py`): ensure `MEMORY_BACKEND_IS_POSTGRES` flag is set BEFORE any node spawns (already true per SPEC-MEMORY-001 — verify no regression).
8. **Tests** (11 files): testcontainers Postgres-based integration for thread propagation, payload shapes, search_done richness, node_error, failsoft, Langfuse xref, card_impression coexistence, privacy delete, GIN index usage, migration up/down. Mock Langfuse and mock Telegram adapter.
9. **Cutover**: deploy migration revision on dev-app Postgres → deploy code → smoke-test the 7 manual scenarios → monitor `ai.log_conversation_event` row growth + `idx_log_conv_payload_gin` size for 24h.

---

## Test Plan Outline (informative — formalized in acceptance.md)

- **Unit (`tests/test_conversation_log/test_log_event.py`)**: happy-path INSERT, in-memory fallback skip, pool failure WARN + stderr fallback, NOT-NULL violation handling, malformed payload handling. ≥ 85% module coverage.
- **Unit (`tests/test_conversation_log/test_thread_propagation.py`)**: webhook → 12 nodes → respond — single thread_id across all rows; uuid4 default value; multi-webhook independence.
- **Unit (`tests/test_conversation_log/test_payload_shapes.py`)**: per-event-type TypedDict construction; required-key presence; `json.dumps(default=str)` success for each.
- **Unit (`tests/test_conversation_log/test_search_payload.py`)**: parallel-array length; empty case; mismatched-length defensive assertion → `node_error` emit; JSON `->` extraction.
- **Unit (`tests/test_conversation_log/test_node_error.py`)**: each of 12 nodes forced to raise → `node_error` row appears with documented payload; `recovered` flag accuracy.
- **Unit (`tests/test_conversation_log/test_failsoft.py`)**: pool patch → no exception, WARN + stderr fallback; 1000-call concurrent property test asserts 100% accountability (inserts + stderr = 1000).
- **Unit (`tests/test_conversation_log/test_langfuse_xref.py`)**: Langfuse v3 active → trace_id populated; no-op fallback → NULL; helper never raises.
- **Unit (`tests/test_conversation_log/test_implicit_fb_coexist.py`)**: 3-card send → 3 + 3 = 6 rows across two tables; click event → flip + append; independence of failures.
- **Unit (`tests/test_conversation_log/test_privacy_delete.py`)**: two-user isolation; index-scan EXPLAIN check; large-volume timing.
- **Unit (`tests/test_conversation_log/test_gin_index.py`)**: `EXPLAIN (FORMAT JSON)` for `@>` queries asserts `Bitmap Index Scan on idx_log_conv_payload_gin`.
- **Unit (`tests/test_conversation_log/test_migration.py`)**: `alembic upgrade head` + downgrade; column / index / constraint snapshot; idempotency under re-run.
- **Integration (cross-test)**: 100-turn synthetic load → ≥ 800 rows in 5 minutes. Forced PG outage mid-turn → bot completes, stderr fallback present.
- **Regression**: full existing `tests/` tree green under both `MEMORY_FALLBACK_ON_PROBE_FAIL=true` (in-memory: conversation_log inert) and `=false` (postgres: conversation_log active).
- **Coverage**: `pytest --cov=app.observability.conversation_log` reports ≥ 85%.
- **End-to-end manual**: the 7 scenarios in the Definition of Done section.
