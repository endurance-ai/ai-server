---
id: SPEC-AGENT-UX-P0-001
version: 0.2.0
status: draft
created: 2026-05-20
updated: 2026-05-20
author: hchsa77@gmail.com
priority: P0
issue_number: null
labels: [agent, ux, telegram, diversify, sticky-lang, typing-indicator, pre-messages, beta-feedback]
---

# SPEC-AGENT-UX-P0-001: Quick-Win UX Fixes — Card Dedup, Sticky Language, Typing Indicator, Pre-Action Messages

## HISTORY

- 2026-05-20 (v0.2.0): REQ-UX-004 추가 — **사전 안내 멘트** (pre-action messages). 베타 사용자 피드백 후속: typing indicator (REQ-UX-003) 만으로는 "뭐가 진행되고 있는지" 인지가 약함. 1~2초 이상 걸리는 작업(Vision 분석 / 상품 검색 / refine / Pinterest 스크랩 / analyze_image) 직전에 고정 한국어/영어 안내 멘트를 먼저 발송해 의도를 명시한다. 멘트는 단일 모듈(`app/channels/pre_messages.py`) 상수 dict 로 관리하며 5개 지점에서 idempotent 1회 호출. fail-open + sticky lang (`session_lang(sess)`) + 텔레그램 전용 — REQ-UX-002/003 정책과 정합. 다른 메신저·LLM 생성 멘트·다국어 확장은 비-스코프.
- 2026-05-20 (v0.1.0): 초안 작성. `@kiko_fashion_ai_bot` 베타 피드백 5종 중 **빠른-win 3종**(동일 상품 중복 카드 / 도중 영어 응답 / typing indicator 부재)을 단일 SPEC 으로 묶음. 직접적 동기는 베타 사용자 관찰: (a) 한 번의 추천에서 같은 product_id 가 카드 2개로 나옴 — `app/services/diversify_service.py` 가 `brand_cap`/`platform_cap` 만 적용하고 product_id 레벨 dedup 이 없음 (v6 RPC 가 동일 id 를 중복 반환하거나 candidate set 이 병합되면 0차 보호망 없음); (b) 한글 1회 발화 후 후속 짧은 발화나 button-tap 에서 봇이 영어로 빠짐 — `app/channels/lang.py::session_lang` 은 정상 sticky 갱신되지만 `app/agents/react_loop.py` 시스템 프롬프트에 LANG 강제 directive 가 없어 LLM 이 ctx 를 무시; (c) 검색·LLM 응답 생성 중 사용자가 "응답이 오긴 오나" 모름 — `app/channels/telegram/adapter.py` 에 `sendChatAction` 호출 메서드 자체가 없음. 베타에서 빈도가 높고 수정 surface 가 명확한 3건만 본 SPEC 에 포함. 별도 사이클로 분리되는 항목: #2 카테고리 미스매치 진단, #4 맥락 혼동, ★취향→랭킹 가중, ★Vision prompt 정련. WHAT/WHY 만 정의 — HOW(정확한 hook 위치, dedup key 함수 시그니처 등)는 `plan.md` 및 Run phase 에서 결정.

---

## Goal

베타 사용자 피드백에서 빈도와 임팩트가 모두 높은 4건의 UX 결함을 단일 quick-win 라운드로 제거한다. 네 결함은 서로 다른 레이어(diversify service / agent system prompt / messenger adapter / pre-action signaling)에 속하지만 모두 "한 번의 코드 변경 + 단위 테스트" 로 닫을 수 있는 좁은 surface 라서 한 SPEC 으로 묶는다.

| 결함 | 사용자 체감 | 현재 코드 갭 | 본 SPEC 의 처방 |
|---|---|---|---|
| 동일 상품이 카드 2개로 노출 | "같은 옷이 두 번 떠요" | `diversify_service` 가 product_id 레벨 dedup 을 안 함 (brand/platform 캡만) | dedup-by-id 가드 추가 |
| 한글 대화 도중 영어 응답 | "한국말로 하다가 갑자기 영어로 답해요" | `Session.lang` 은 sticky 갱신되지만 LLM 시스템 프롬프트에 LANG 강제 directive 부재 | `[LANG=ko|en — MUST reply in …]` 펜스 시스템 프롬프트 주입 |
| 응답 생성 중 무반응 | "응답이 오긴 오나?" | TelegramAdapter 에 `sendChatAction` 메서드 자체 없음 | adapter ABC + telegram 구현 추가, search/refine/respond 진입부에서 fail-open 호출 |
| 뭐가 진행 중인지 모름 (REQ-UX-004) | "Typing 표시는 보이는데 뭘 하고 있는지 모르겠어요" | Vision / 검색 / refine / Pinterest 스크랩 / analyze_image 진입부에 의도-안내 멘트 없음 | `pre_messages.py` 단일 소스 + 5개 지점에서 idempotent 사전 안내 멘트 발송 |

세 변경 모두:

1. **외부 토폴로지 무변경.** 그래프 노드 추가/삭제 없음, 새 env var 없음, 새 DB 테이블 없음.
2. **Fail-open.** dedup 가드는 id 누락 candidate 를 통과시키고, LANG directive 는 누락 시 봇 동작 byte-identical, typing indicator 호출은 예외 swallow.
3. **다른 채널 어댑터 무영향.** ABC 의 default `False` 반환으로 미구현 채널은 자동 skip — 본 SPEC 의 구현 surface 는 Telegram only.

이 SPEC 은 **WHAT** 과 **WHY** 만 정의한다. 정확한 dedup key 함수 시그니처, LANG directive 펜스의 정확한 라인 위치, typing indicator hook 의 정확한 dispatch 진입점 — 모두 `plan.md` 와 Run phase 에서 결정한다.

---

## Background

### 결함 #1 — 동일 상품 중복 카드 (REQ-UX-001 의 동기)

`app/services/diversify_service.py` 의 캡 루프(line 51~64)는 다음 두 가드만 갖는다:

```python
for c in state.raw_candidates:
    brand = (c.get("brand") or "").lower()
    platform = (c.get("platform") or "").lower()
    if seen_brand.get(brand, 0) >= brand_cap: ...
    if seen_platform.get(platform, 0) >= platform_cap: ...
    out.append(c)
```

`c["id"]` (product_id) 가 같은 row 두 개가 raw_candidates 에 동시에 존재하면 둘 다 `out` 에 들어간다. 이 케이스가 실제로 발생하는 경로:

- v6 embedding-first RPC 가 동일 product 를 distance tie 로 두 번 반환 (드물지만 관측됨).
- ReAct loop 의 `refine_search` 가 cumulative result set 을 머지할 때 prior turn 의 candidate 와 신규 turn 의 candidate 가 같은 id 를 포함.

`brand_cap`/`platform_cap` 으로는 잡히지 않는다 — 동일 id 라도 brand·platform 카운터는 각각 1씩 올라가므로 cap 한도에 닿지 않는다. 카드 5개 중 2개가 같은 옷으로 나가는 것은 추천 시스템 신뢰도의 즉각 손상.

### 결함 #2 — Sticky language 강제 (REQ-UX-002 의 동기)

`app/channels/lang.py` 의 `detect_lang` / `remember_lang` / `session_lang` 는 정상 동작:

- 매 텍스트 턴마다 Hangul 유무로 KO/EN 판별.
- `Session.lang` 에 sticky 저장 — 이후 button-tap(텍스트 없음) 에도 직전 언어 유지.
- `respond` tool (`send_hybrid_batch`) 와 `pick_item`/`ask_clarify` 노드가 `session_lang(sess)` 를 참조해 KO/EN 분기.

그러나 **ReAct loop 의 LLM 시스템 프롬프트(`app/agents/react_loop.py` line 411~417)** 에 LANG 강제 directive 가 없다. 시스템 프롬프트는 `_SYSTEM_PROMPT + _PROACTIVE_DIRECTIVE + memory_context` 의 조합으로만 구성되며, ctx 의 `lang` 정보는 LLM 에 명시 전달되지 않는다. 결과:

- LLM 이 자유 텍스트를 만들 때 영어로 흐르는 경향 (kiko 페르소나가 lively English 도 허용).
- 한글 1회 발화 후 후속 짧은 발화 ("응", "ok", "그래") 또는 button-tap 시 봇 응답이 영어로 빠짐.

`respond` tool 안에서는 `session_lang(sess)` 가 KO 로 잡혀 hybrid 카드 텍스트는 KO 로 나가지만, agent 가 자유 응답을 만드는 path 에서는 LLM 이 시스템 프롬프트의 부재로 인해 자체 판단으로 영어를 선택.

### 결함 #3 — Typing indicator 부재 (REQ-UX-003 의 동기)

`app/channels/telegram/adapter.py` (448 LOC) 와 ABC `app/channels/adapter.py` (74 LOC) 어디에도 `sendChatAction` 호출 메서드가 없다. Telegram Bot API 의 `sendChatAction(chat_id, action="typing")` 은:

- 한 번 호출하면 5초 동안 "kiko 입력 중…" 인디케이터 표시 (또는 다음 메시지 도착 시까지 둘 중 빠른 쪽).
- 단발 호출로 충분 — 주기 재전송 불필요.

봇이 search 호출 (수초) → LLM 응답 생성 (수초) 동안 사용자는 채팅창에 어떤 신호도 받지 못한다. 베타 사용자 관찰: "응답이 오긴 오나" 라고 의심하며 다른 메시지를 보내거나 채팅창을 떠남.

### 왜 한 SPEC 에 묶는가

세 결함은 다른 모듈에 속하지만 공통점:

- 각각 단일 함수/메서드 추가 또는 한두 라인 변경.
- 각각 fail-open 보장이 가능 (graceful degrade).
- 각각 단위 테스트 1-3개로 acceptance 가능.
- 셋 다 베타에서 빈도 높음 — 다음 사용자 라운드까지 동시 머지 가치.

별도 SPEC 으로 쪼개면 PR 3개 + 회귀 테스트 회전 3회 — quick-win 의 의도와 어긋남.

---

## Architecture Snapshot (informative)

Today (pre-SPEC):

```
[diversify] state.raw_candidates → loop with brand_cap / platform_cap → out[]
            ❌ no product_id dedup → duplicate cards possible

[react_loop] system_prompt = _SYSTEM_PROMPT + _PROACTIVE_DIRECTIVE + memory_context
             ❌ no LANG directive → LLM may reply in EN even when session.lang=ko

[telegram adapter] sendMessage / sendPhoto / sendMediaGroup / InlineKeyboard / edit_inline_keyboard
                   ❌ no sendChatAction method → user sees no "typing" indicator
```

After this SPEC:

```
[diversify]
  for c in state.raw_candidates:
    pid = c.get("id")
    if pid and pid in seen_ids: drops_dup += 1; continue   # NEW
    # ... existing brand/platform caps ...
    out.append(c); seen_ids.add(pid)                       # NEW

[react_loop]
  lang = session_lang(sess)                                # already exists
  lang_directive = f"[LANG={lang} — MUST reply in {LANG_NAME[lang]}]"   # NEW
  system_content = f"{_SYSTEM_PROMPT}\n\n{_PROACTIVE_DIRECTIVE}\n\n{lang_directive}"
  # respond tool unchanged — already uses session_lang(sess)

[telegram adapter]
  async def send_chat_action(chat_id, action="typing") -> bool:   # NEW
    # POST sendChatAction, fail-open swallow

[react_loop tool dispatch]
  if tool_name in ("search_products", "refine_search"):
    asyncio.create_task(adapter.send_chat_action(chat_id))        # NEW, fail-open
  # respond tool: send_chat_action 1회 직전에 호출 (텍스트 전송 직전)
```

**Affected modules in kikoai/ai** (exact filenames refined in `plan.md`):

- `app/services/diversify_service.py` — MODIFIED. dedup-by-id guard at loop entry + `drops_dup` counter in step 4.8 log line.
- `app/agents/react_loop.py` — MODIFIED. `lang_directive` 라인 시스템 프롬프트에 append (메모리 컨텍스트 펜스와 동일 패턴). `respond` tool 진입부에서 typing-indicator 호출 (text send 직전 1회).
- `app/agents/react_loop.py` (또는 tool dispatch helper) — MODIFIED. `search_products` / `refine_search` tool dispatch 직전에 `send_chat_action` 호출 (fail-open `asyncio.create_task`).
- `app/channels/adapter.py` — MODIFIED. ABC 에 `async def send_chat_action(chat_id: int, action: str = "typing") -> bool` 추가 (default 구현은 `return False`).
- `app/channels/telegram/adapter.py` — MODIFIED. 실제 POST sendChatAction 구현 (httpx, fail-open swallow, debug log).
- `tests/test_diversify/test_diversify_dedup.py` — NEW. dedup unit test.
- `tests/test_agents/test_react_loop_lang_directive.py` — NEW. KO/EN 양방향 시스템 프롬프트 스냅샷 테스트.
- `tests/test_channels/test_telegram_chat_action.py` — NEW. httpx 모킹으로 sendChatAction endpoint 호출 검증.
- `tests/test_agents/test_react_loop_typing_hook.py` — NEW. search/refine tool dispatch 직전 1회 호출 검증.

**Reused, untouched modules**:

- `app/channels/lang.py` — `detect_lang` / `remember_lang` / `session_lang` 무변경.
- `app/channels/persona.py` — kiko 페르소나 프롬프트 무변경 (LANG directive 는 별도 라인으로 append).
- `app/services/{search_service,embed_service,database_service}.py` — 무관.
- `app/infrastructure/repositories/search_repository.py` — 무관.
- 다른 채널 어댑터 (현재 Telegram only) — ABC default `False` 로 자동 skip.

---

## Requirements & Acceptance Criteria

### REQ Index

| REQ-ID | Title | Priority |
|---|---|---|
| REQ-UX-001 | Diversify SHALL deduplicate candidates by `id` before applying brand/platform caps | P0 |
| REQ-UX-002 | ReAct loop SHALL inject sticky language directive into the LLM system prompt | P0 |
| REQ-UX-003 | Telegram adapter SHALL expose `send_chat_action`; ReAct loop SHALL invoke it at search/refine dispatch and respond send | P0 |
| REQ-UX-004 | System SHALL send a fixed pre-action message at 5 long-running entry points (Vision / search / refine / Pinterest scrape / analyze_image) before executing the underlying work | P0 |

---

### Diversify Dedup (REQ-UX-001)

#### REQ-UX-001 — Diversify SHALL deduplicate candidates by `id` before the brand/platform cap loop emits a row [P0]

**WHEN** `diversify_service` iterates `state.raw_candidates`,
**THE SYSTEM SHALL** maintain a `seen_ids: set[str]` keyed on each candidate's `id` field AND SHALL NOT append a candidate to `out` whose `id` is already in `seen_ids`.

**WHERE** a candidate is missing the `id` field (`c.get("id")` is `None` or empty string),
**THE SYSTEM SHALL** treat the candidate as un-dedupable (i.e., bypass the dedup guard, but still subject to brand/platform caps) so that data shape regressions in upstream RPC responses degrade gracefully instead of dropping every such row.

**THE SYSTEM SHALL** add a `drops_dup` counter incremented on each dedup-rejection AND include it in the existing `[STEP 4.8][diversify] 끝` log line alongside `drops_brand` / `drops_platform`.

**Rationale**: Current code (`app/services/diversify_service.py` L51-64) caps only by `brand` and `platform`. Distance-tie duplicates from `search_products_v6` and cross-turn candidate merges by `refine_search` slip through, producing visible duplicate cards in the user's carousel. Dedup-by-id is the cheapest fix at the lowest defensible layer (just before user-facing dispatch).

**Acceptance**:

- A unit test seeds `state.raw_candidates` with 5 dicts, 2 of which share the same `id` (`"prod-A"`). After `diversify_service(state)` the resulting `state.final_candidates` contains exactly 4 entries with `id` values forming a set of size 4 (no `"prod-A"` duplicate).
- A unit test seeds 3 candidates where one has `id=None` and another has `id=""`. The dedup guard does NOT collapse them — both pass to the brand/platform stage (asserts: `out` length >= 2 for the missing-id pair, modulo brand/platform caps).
- A unit test asserts the existing brand/platform cap semantics are byte-identical when no dedup-eligible duplicates exist: a 10-candidate input with unique ids and a brand cap of 3 produces the SAME `out` (count, order, contents) as the pre-SPEC code.
- A unit test asserts `drops_dup` is incremented for every dedup rejection AND appears in the `[STEP 4.8]` log line (captured via `caplog`).
- The dedup guard SHALL NOT change the early-exit behavior (`if len(out) >= target: break`).
- The dedup guard SHALL NOT mutate the input `state.raw_candidates` list.

---

### Sticky Language Directive (REQ-UX-002)

#### REQ-UX-002 — ReAct loop SHALL inject `[LANG=<ko|en> — MUST reply in <Korean|English>]` directive into the LLM system prompt [P0]

**WHEN** `run_react_loop` (`app/agents/react_loop.py`) builds the LLM `system_content` for a turn,
**THE SYSTEM SHALL** resolve the current session language via `session_lang(sess)` (already imported from `app/channels/lang.py`) AND append exactly one directive line to the system prompt in the form:

```
[LANG=ko — MUST reply in Korean]
```

or

```
[LANG=en — MUST reply in English]
```

The directive SHALL be appended AFTER the existing `_SYSTEM_PROMPT + _PROACTIVE_DIRECTIVE + memory_context` assembly so it is the last directive the LLM sees before the user message fence.

**WHEN** the `respond` tool dispatches text via `send_hybrid_batch` (or any future text-only fallback path),
**THE SYSTEM SHALL** continue to use `session_lang(sess)` as it already does (no new work for respond; this clause is here only to lock the invariant that LANG resolution is single-sourced through `session_lang`).

**Acceptance**:

- A snapshot test asserts the assembled `system_content` for a `Session(lang="ko")` ends with the literal line `[LANG=ko — MUST reply in Korean]`. The line MUST appear after the memory-context block.
- A snapshot test for `Session(lang="en")` asserts the literal line `[LANG=en — MUST reply in English]`.
- A regression test asserts the `_SYSTEM_PROMPT`, `_PROACTIVE_DIRECTIVE`, and memory-context contents themselves are unchanged (the directive is appended, not interleaved).
- An integration-style test (using a fake LLM that echoes a fixed string) asserts that after a Korean turn ("한글이야") the system prompt of the NEXT turn still carries `[LANG=ko — MUST reply in Korean]` even when the next turn's user payload is a button-tap callback with empty text (sticky validation).
- The exact wording of the directive (whether to use em-dash, the literal "MUST", the exact KO/EN names) is locked by this REQ to allow snapshot tests to be brittle on purpose — drift in the directive is a SPEC-level change.
- No new env var is introduced. No feature flag. The directive is unconditional (consistent with SPEC-AGENT-V2-CLEANUP-001's "no feature flags" policy).

---

### Typing Indicator (REQ-UX-003)

#### REQ-UX-003 — Telegram adapter SHALL expose `send_chat_action`; ReAct loop SHALL call it at search/refine tool dispatch and just before respond text send [P0]

**THE SYSTEM SHALL** add an abstract async method to `app/channels/adapter.py::MessengerAdapter`:

```python
async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool: ...
```

with a default ABC implementation that returns `False` (no-op). Non-Telegram adapter implementations MAY leave the default in place (REQ-UX-003 is Telegram-only delivery).

**THE SYSTEM SHALL** add a concrete implementation in `app/channels/telegram/adapter.py::TelegramAdapter.send_chat_action` that POSTs to the Telegram Bot API `sendChatAction` endpoint with the given `chat_id` and `action`. The implementation SHALL be **fail-open**: all exceptions (network, timeout, HTTP non-2xx) are swallowed; a `logger.debug` line is emitted; the method returns `False` on failure and `True` on success.

**WHEN** the ReAct loop dispatches the `search_products` or `refine_search` tool,
**THE SYSTEM SHALL** invoke `send_chat_action(chat_id, "typing")` exactly once via `asyncio.create_task` (fire-and-forget) before the tool body executes.

**WHEN** the `respond` tool prepares to send text (immediately before `send_hybrid_batch` or any text-only send path),
**THE SYSTEM SHALL** invoke `send_chat_action(chat_id, "typing")` exactly once via `asyncio.create_task` (fire-and-forget).

**Rationale**: Telegram's `sendChatAction` displays a "typing…" indicator for ~5 seconds, signaling that the bot is alive while it performs the (sometimes multi-second) embedding → RPC → LLM pipeline. A single call covers the typical search round; periodic re-transmission is unnecessary and out of scope (see Exclusions). Fail-open semantics ensure a Telegram API hiccup never blocks the user response.

**Acceptance**:

- A unit test asserts the `MessengerAdapter` ABC defines `send_chat_action` with the signature `async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool`. A non-Telegram fake adapter using the ABC default returns `False` without raising.
- A unit test uses an httpx `MockTransport` (or `respx`) to assert `TelegramAdapter.send_chat_action(chat_id=42)` POSTs to `https://api.telegram.org/bot<TOKEN>/sendChatAction` with the JSON body `{"chat_id": 42, "action": "typing"}` and returns `True` on `{"ok": true}` response.
- A fail-open unit test asserts `send_chat_action` returns `False` (no raise) when the mocked transport returns HTTP 500, when it raises `httpx.TimeoutException`, and when it raises a generic `httpx.HTTPError`. In each case exactly one DEBUG-level log line is emitted; no WARN or ERROR.
- A unit test asserts the ReAct loop dispatches `send_chat_action` exactly once before `search_products` tool body and exactly once before `refine_search` tool body (uses a spy adapter that records call count + `action` parameter). The `asyncio.create_task` wrapping pattern means the typing call does NOT block the tool dispatch — verified by timing assertion (tool body completes in body-only time).
- A unit test asserts the `respond` tool invokes `send_chat_action` exactly once before its text send (count == 1 per turn that reaches respond).
- The Telegram API call SHALL use the existing httpx client / settings (`TELEGRAM_BOT_TOKEN`, timeout) that the adapter already uses for `sendMessage` — no new client construction.
- The typing call SHALL NOT be retried on failure (best-effort signal).
- The typing call SHALL NOT be reissued periodically (single shot per search/refine dispatch and per respond send; Telegram's 5-second display window is sufficient for typical turns; longer turns will simply lose the indicator until the next tool dispatch).

---

### Pre-Action Messages (REQ-UX-004)

#### REQ-UX-004 — System SHALL send a fixed pre-action message at 5 long-running entry points before executing the underlying work [P0]

**THE SYSTEM SHALL** maintain a single-source dictionary of fixed pre-action messages in a new module `app/channels/pre_messages.py` of the form:

```python
PRE_MESSAGES: dict[str, dict[str, str]] = {
    "vision":          {"ko": "사진 잘 봤어요, 잠깐 분석해볼게요 👀", "en": "Got it! Let me take a closer look 👀"},
    "search":          {"ko": "잠시만요, 마음에 들 만한 거 찾아볼게요 🔍", "en": "One sec — let me find something you'll love 🔍"},
    "pinterest":       {"ko": "보드 살펴볼게요, 잠시만요 📌",     "en": "Checking out your board, hang tight 📌"},
    "analyze_image":   {"ko": "이미지 들여다볼게요 👀",          "en": "Looking at this image 👀"},
}
```

The `search` key SHALL serve **both** the `search_products` tool dispatch and the `refine_search` tool dispatch (same message — user-visible action is identical).

**WHEN** any of the following 5 code sites is entered, **THE SYSTEM SHALL** resolve the current session language via `session_lang(sess)` AND send `PRE_MESSAGES[<key>][<lang>]` to the user via `adapter.send_text` BEFORE invoking the underlying long-running operation:

| # | Entry Point | Code Location | Pre-Message Key | Firing Position |
|---|---|---|---|---|
| A | Vision graph node | `app/graphs/nodes/vision.py` (node entry) | `vision` | Immediately before the LiteLLM Vision call |
| B | `search_products` tool | `app/agents/tools/search_products.py` (dispatch entry) | `search` | Immediately before `run_image_search` / `run_text_only_search` |
| C | `refine_search` tool | `app/agents/tools/search_products.py` (refine dispatch entry) | `search` | Immediately before refine search execution |
| D | Pinterest scrape node | `app/graphs/nodes/pinterest_ingest.py` (node entry) | `pinterest` | Immediately before the Apify scrape call |
| E | `analyze_image` tool | `app/agents/tools/analyze_image.py` (dispatch entry) | `analyze_image` | Immediately before the LiteLLM Vision call |

**Firing ordering invariant** (per turn, per entry point):

1. Pre-action message sent (`adapter.send_text` with `PRE_MESSAGES[key][lang]`).
2. Typing indicator fired (REQ-UX-003 — `asyncio.create_task(adapter.send_chat_action(...))`).
3. Long-running operation begins.

The pre-action message MUST appear BEFORE the typing indicator so the user reads the bot's intent statement first, then sees the "typing…" affordance while the work runs.

**WHERE** the `send_text` call fails (network, timeout, HTTP non-2xx),
**THE SYSTEM SHALL** swallow the exception, emit exactly one `logger.debug` line, AND continue with the underlying operation. The pre-action message is a best-effort UX signal — a send failure SHALL NOT block or delay the user's actual result.

**Idempotency**: WHEN the same entry point is dispatched more than once within a single turn (e.g., ReAct loop re-entering `search_products` after an LLM retry, or graph node re-execution), **THE SYSTEM SHALL** send the pre-action message AT MOST ONCE per entry-point-per-turn. The dedup marker SHALL be stored on:

- The `ctx` dict (for tool dispatches in `react_loop.py`) under a key of the form `_pre_msg_sent:{tool_name}`, OR
- The graph state (for graph nodes — `vision_node`, `pinterest_ingest`) under a key of the form `_pre_msg_sent:{node_name}`.

The exact dedup-marker storage convention (ctx vs state) is decided in `plan.md`; the SPEC locks only the **invariant** (one send per entry-point-per-turn) and the **acceptance shape** (a unit test asserts second dispatch in the same `ctx` does not re-send).

**Language**: `session_lang(sess)` SHALL be the single source of truth — identical to REQ-UX-002's sticky-lang policy. No new lang resolution path is introduced.

**Telegram-only delivery**: `adapter.send_text` is part of the existing `MessengerAdapter` ABC and is already implemented by `TelegramAdapter`. Other (future) channel adapters inherit the existing send_text default; no new abstract method is added for pre-action messages.

**Rationale**: Beta users reported that the typing indicator (REQ-UX-003) provides only a generic "kiko 입력 중…" affordance — useful for "is the bot alive?" but not for "what is the bot DOING?". A concise intent statement ("사진 잘 봤어요, 잠깐 분석해볼게요 👀") immediately before each long-running phase tells the user *why* they are waiting and makes the multi-second pipeline feel intentional rather than slow. Fixed wording (not LLM-generated) is chosen for predictability, snapshot-testability, and zero added latency. Single-sourcing in `pre_messages.py` makes copy revisions a one-line diff.

**Acceptance**:

- A snapshot test asserts `PRE_MESSAGES` contains exactly the 4 keys `vision`, `search`, `pinterest`, `analyze_image`, AND each key has both `ko` and `en` sub-keys, AND each value is a non-empty string. Drift in keys or empty values is a SPEC-level change (intentional brittleness).
- A unit test asserts entering `vision_node` invokes `adapter.send_text` exactly once with text equal to `PRE_MESSAGES["vision"][lang]` (where `lang` is resolved from the session). The Vision call (LiteLLM) executes only AFTER the `send_text` invocation.
- A unit test asserts dispatching `search_products` invokes `adapter.send_text` exactly once with `PRE_MESSAGES["search"][lang]`. Same assertion for `refine_search` — both use the `search` key (identical message).
- A unit test asserts dispatching `analyze_image` invokes `adapter.send_text` exactly once with `PRE_MESSAGES["analyze_image"][lang]`.
- A unit test asserts entering `pinterest_ingest` invokes `adapter.send_text` exactly once with `PRE_MESSAGES["pinterest"][lang]`.
- An idempotency test asserts two consecutive `search_products` dispatches sharing the same `ctx` dict result in `adapter.send_text` being called exactly ONCE (second dispatch reads the `_pre_msg_sent:search_products` marker and skips). Same pattern for the other 4 entry points.
- A language sticky test asserts `Session(lang="ko")` produces the KO variant and `Session(lang="en")` produces the EN variant for every entry point (parametric over 5 entry points × 2 languages = 10 assertions).
- A fail-open test asserts that when `adapter.send_text` raises (network / timeout), the underlying long-running operation still executes (e.g., the Vision call is still made) and exactly one DEBUG log line is emitted. No WARN, no ERROR.
- A firing-order test asserts that within a single `search_products` dispatch, `adapter.send_text` (REQ-UX-004) is called BEFORE `adapter.send_chat_action` (REQ-UX-003). Spy adapter records the call order.
- Regression: every test introduced by REQ-UX-001 / REQ-UX-002 / REQ-UX-003 continues to pass unchanged.
- No new env var is introduced. No feature flag. The pre-action messages are unconditional (consistent with SPEC-AGENT-V2-CLEANUP-001's "no feature flags" policy).

---

## Exclusions (What NOT to Build)

The following are explicitly NOT delivered by SPEC-AGENT-UX-P0-001 and MUST NOT be conflated with it:

1. **Merging text and cards into a single Telegram message.** Out of scope — the current hybrid batch path (`sendMediaGroup` + separate caption / keyboard) stays as-is.
2. **Periodic re-transmission of `sendChatAction`.** Single shot only. Telegram's 5-second display window is accepted as the bound. Long-running turns that exceed 5 seconds will simply stop showing the indicator until the next tool dispatch — no looped re-send.
3. **Typing indicator for non-Telegram channels.** Other adapter implementations inherit the ABC default `False` (no-op). REQ-UX-003 delivery is Telegram-only.
4. **Typing indicator for non-search tools.** `analyze_image`, `update_taste`, `ask_user_clarification`, `get_recent_history`, `suggest_next_step` do NOT trigger typing calls (only `search_products` / `refine_search` / `respond`).
5. **Category-mismatch diagnostic (beta feedback #2).** Separate SPEC.
6. **Multi-turn context confusion fix (beta feedback #4).** Separate SPEC.
7. **Taste-profile → ranking weight integration (★ item).** Separate SPEC.
8. **Vision prompt refinement (★ item).** Separate SPEC.
9. **Cross-session dedup (e.g., "don't show product X again ever").** Out of scope — dedup is per-turn only, scoped to `state.raw_candidates` within one `diversify_service` call.
10. **New env vars or feature flags.** Three changes are unconditional. Consistent with SPEC-AGENT-V2-CLEANUP-001's "no flags" policy.
11. **LANG directive auto-translation of bot persona.** The `kiko` persona prompt (`app/channels/persona.py`) is unchanged. Only the appended LANG directive line is added; the LLM is trusted to translate persona voice itself based on the directive.
12. **Dedup of `state.final_candidates` after diversify.** The guard is applied DURING the diversify loop only. Downstream consumers (impression logging, respond) trust `final_candidates` to already be deduped.
13. **Telemetry / Langfuse spans for the new calls.** The dedup counter goes into the existing log line (`drops_dup`); typing-indicator calls are debug-log only. No new Langfuse span types.
14. **Backfill or replay of past sessions for dedup.** SPEC takes effect at merge time; past `card_impression` rows are not retroactively deduped.
15. **Pre-action messages for fast/terminal tools (REQ-UX-004 scope).** `update_taste`, `ask_user_clarification`, `get_recent_history`, `respond`, `suggest_next_step` do NOT trigger pre-action messages. These complete in well under 1s and a pre-message would feel noisy, not informative.
16. **Multi-language expansion of pre-action messages (REQ-UX-004 scope).** KO and EN only. No `ja` / `zh` / etc. Consistent with `app/channels/lang.py::session_lang` which returns only `"ko" | "en"`.
17. **A/B testing, variants, or LLM-generated wording for pre-action messages.** Fixed strings only. The single-source dict in `pre_messages.py` is the only place wording is defined; changes are SPEC-level (snapshot tests break on drift).
18. **Periodic / intermission "still working" pre-messages.** Single shot per entry-point-per-turn. No "아직 진행중…" follow-up messages — typing indicator (REQ-UX-003) covers the alive-signal need; pre-message covers the intent-signal need; the two together are deemed sufficient.
19. **Pre-action messages for non-Telegram channels.** Delivery is Telegram-only. The existing `adapter.send_text` ABC method is inherited by other (future) adapters but REQ-UX-004 firing sites are introduced only in code paths reached by the Telegram pipeline.
20. **Cross-turn idempotency for pre-action messages.** Idempotency is per-turn only. If the user uploads two photos in two consecutive turns, both turns SHALL emit the `vision` pre-message — the marker on `ctx` / state is turn-scoped and reset on new turn entry.

---

## Stakeholders

| Role | Responsibility |
|---|---|
| Product / Founder (hchsa77@gmail.com) | Identified the three beta complaints (v0.1.0) and the follow-up "intent signal" gap (v0.2.0 REQ-UX-004). Confirms the quick-win bundling (vs splitting into four SPECs). Owns the decision to scope out the four follow-up items (#2, #4, ★ranking, ★vision prompt) to separate cycles. Owns the fixed pre-message wording. |
| AI Server Owner (this SPEC) | All work in `app/services/diversify_service.py` (MODIFIED), `app/agents/react_loop.py` (MODIFIED), `app/channels/adapter.py` (MODIFIED), `app/channels/telegram/adapter.py` (MODIFIED), `app/channels/pre_messages.py` (NEW), `app/graphs/nodes/vision.py` (MODIFIED), `app/graphs/nodes/pinterest_ingest.py` (MODIFIED), `app/agents/tools/search_products.py` (MODIFIED), `app/agents/tools/analyze_image.py` (MODIFIED). Owns the new test files including REQ-UX-004 coverage. |
| Telegram Bot API | Out of scope — black-boxed. The `sendChatAction` and `sendMessage` endpoint contracts are stable (long-documented). |
| Modal / kikoai/app teams | Out of scope. |

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | **Dedup guard accidentally drops legitimate candidates** (e.g., if `id` is coincidentally shared by different products due to an upstream schema bug). | Low | Medium | The guard only fires when `c.get("id")` is truthy AND already in `seen_ids`. Falsy ids (None / `""`) bypass dedup — graceful fallback. Existing tests for diversify behavior (`tests/test_diversify/`) re-run unchanged. New regression test asserts byte-identical behavior on unique-id input. |
| R2 | **LANG directive ignored by LLM** (Bedrock nova-lite / Haiku 4.5 may de-prioritize the directive in long contexts). | Medium | Low | The directive is placed LAST in the system prompt (highest recency for transformer attention). Snapshot tests assert the literal line is present. If observed compliance is still low post-merge, follow-up: move directive INTO the user-message fence (which has even higher recency). That follow-up is out of scope of this SPEC. |
| R3 | **`sendChatAction` rate limit.** Telegram throttles `sendChatAction` per chat per second. Multiple rapid tool dispatches (search → refine → search) could trip the limit. | Low | Low | (a) Per-turn call count is bounded: max 1 per search/refine + 1 at respond = typically 2-3 per turn. (b) Fail-open swallow means any 429 is silently absorbed — user just doesn't see the indicator. (c) `asyncio.create_task` fire-and-forget means the rate limit never propagates to the user response path. |
| R4 | **httpx client reuse.** `send_chat_action` SHOULD reuse the existing httpx client to avoid connection churn. | Low | Low | `plan.md` decides the exact client-acquisition pattern (likely a property on TelegramAdapter that mirrors the existing `sendMessage` path). A test asserts no new client is constructed per call. |
| R5 | **Test brittleness on snapshot strings.** REQ-UX-002 acceptance locks the exact directive wording (`[LANG=ko — MUST reply in Korean]`). Any future copy edit to the directive breaks the snapshot. | Medium | Low | Intentional — the brittleness is the test's value. Directive wording is a SPEC-level decision. Any change requires a SPEC version bump. |
| R6 | **Typing indicator fires at unrelated callbacks.** If REQ-UX-003 is over-applied and `send_chat_action` is hooked at every tool dispatch, lightweight tools (`update_taste`, `get_recent_history`) would emit unnecessary "typing…" — confusing UX (no actual reply incoming). | Medium | Medium | REQ-UX-003 SHALL fire only at `search_products` / `refine_search` / `respond` — explicitly listed. AST test enumerates the 3 sites and asserts no other dispatch invokes `send_chat_action`. |
| R7 | **In-flight typing task leaks on container shutdown.** `asyncio.create_task` returns a coroutine that runs on the event loop; SIGKILL ends the loop instantly. | Low | Low | Accepted. The lost call is a missing UX hint, not a data correctness issue. Consistent with SPEC-CONVERSATION-LOG-001 R5 trade-off. |
| R8 | **Sticky LANG override desire.** Users may want to switch language mid-conversation by typing in the new language. | Low | Low | `Session.lang` is already updated on every text turn by `remember_lang` (per CLAUDE.md). REQ-UX-002 re-reads via `session_lang(sess)` per turn — switch is automatic. No additional UX needed. |
| R9 | **Pre-action message noise** (REQ-UX-004) — if firing sites accidentally overlap (e.g., `search_products` dispatch fires inside a graph node that ALSO fires its own pre-message), user sees redundant "잠시만요…" messages back-to-back. | Medium | Low | Idempotency marker (`_pre_msg_sent:{key}` on ctx/state) prevents re-send within the same turn. The 5 sites are non-overlapping by design (Vision graph node vs analyze_image tool are different code paths — never both in one turn). AST-level test (REQ-UX-004 acceptance) enumerates the 5 firing sites and asserts no other code path imports `PRE_MESSAGES`. |
| R10 | **Pre-message send latency** (REQ-UX-004) — `adapter.send_text` adds one Telegram round-trip (~100-300ms) before the long-running operation, slightly increasing perceived latency to the first card. | Medium | Low | Acceptable trade-off: the perceived latency is masked by the now-visible intent ("잠시만요, 찾아볼게요"). Net UX is positive (typing without intent felt longer per beta feedback). If post-merge data shows regression in P50 latency-to-first-card, follow-up: fire pre-message via `asyncio.create_task` (fire-and-forget like REQ-UX-003) — current SPEC keeps it sequential for ordering guarantee (pre-msg before typing before result). That follow-up is out of scope. |
| R11 | **Pre-message wording drift** (REQ-UX-004) — copy edits over time to the 4 entries × 2 langs = 8 strings break snapshot tests, increasing PR friction. | Low | Low | Intentional. The brittleness is the test's value — wording is a product decision and any change goes through a SPEC version bump (same policy as REQ-UX-002 directive). Single-sourcing in `pre_messages.py` makes the edit a 1-line diff. |

---

## Open Questions (deferred to plan.md / implementation)

본 SPEC 단계에서 의도적으로 deferred. 본 SPEC 승인을 막지 않지만 코드 작성 전 plan.md 에서 결정해야 한다:

1. **Dedup key extraction.** `c.get("id")` vs `c["id"]` vs a typed helper `extract_product_id(c) -> str | None`. plan.md 가 결정 — 단순 dict.get 권장.
2. **LANG directive placement order.** Memory context 다음 라인 vs memory context 이전. 본 SPEC 은 "after memory_context" 로 권고하지만 — plan.md 가 LLM 응답 품질 실측 후 확정 가능. 단, snapshot 테스트는 "directive 는 last line" 을 lock.
3. **Typing indicator hook 위치.** `react_loop.py` 내 dispatch helper 의 정확한 hook point — `_dispatch_tool` 진입 분기인지, tool wrapper 내부인지. plan.md 가 결정 (AST test 가 site 를 검증).
4. **TelegramAdapter httpx client 재사용 패턴.** `sendMessage` 와 같은 client property 를 사용할지, 별도 lightweight client 인지. plan.md 가 결정 (R4).
5. **다른 어댑터의 default 구현.** ABC 가 `return False` 인지, `raise NotImplementedError` 인지. 본 SPEC 은 `return False` 로 권장 (다른 채널 어댑터가 자동 skip 되도록) — plan.md 가 lock.
6. **`drops_dup` 로그 라인 포맷.** 기존 `[STEP 4.8][diversify] 끝 — out=%d drops_brand=%d drops_platform=%d` 에 `drops_dup=%d` 를 어디 위치에 넣을지. plan.md 가 결정 (단순 trailing append 권장).
7. **Pre-message idempotency marker 저장 위치** (REQ-UX-004 OQ-7). Tool dispatch path 는 `ctx` dict (`ctx["_pre_msg_sent:search_products"] = True`) 이 자연스러우나, graph node 진입 (`vision_node`, `pinterest_ingest`) 은 `WorkingState` 의 별도 필드인지 dict 인지 — plan.md 가 결정. SPEC 은 invariant("한 entry-point-per-turn 에 한 번") 만 lock.
8. **Pre-message firing 의 동기성 (REQ-UX-004 OQ-8).** SPEC 은 "send_text 가 typing 보다 먼저, 그리고 본 작업보다 먼저" 만 lock — 실제로는 `await adapter.send_text(...)` (동기) vs `asyncio.create_task(adapter.send_text(...))` (비동기) 의 선택은 plan.md 가 결정. 권장: `await` (ordering 보장 우선, ~100-300ms 비용 수용). 비동기 전환은 R10 follow-up 으로 분리.
9. **Pre-message hook 의 위치** (REQ-UX-004 OQ-9). Tool dispatch path 에서 REQ-UX-003 의 `_fire_typing` helper 옆에 `_fire_pre_message` helper 를 동치로 두는지, 또는 단일 `_fire_pre_action_signals(ctx, key)` 로 묶는지. plan.md 가 결정 — 단일 helper 권장 (ordering 보장 + 단위 테스트 단순화). 단, graph node 진입은 helper 호출이 아닌 node body 첫 줄 inline (graph state 마커 직접 set).

---

## Cross-References

- **Builds on (HARD)**:
  - SPEC-ARCH-AI-001 — `app/services/diversify_service.py` 가 본 SPEC 의 변경 site. `byte-identical` 가드(REQ-AI-007) 는 dedup 가드가 추가될 때 유지된다 (regression test 가 unique-id input 에서 byte-identical 을 assert).
  - SPEC-AGENT-V2-CLEANUP-001 — "no feature flags / single permanent topology" 정책. 본 SPEC 의 세 변경 모두 unconditional, 플래그 없음.
- **Builds on (SOFT)**:
  - SPEC-MSG-001 — `MessengerAdapter` ABC + factory 패턴. 본 SPEC 은 ABC 에 새 메서드만 추가 (default 구현이 다른 어댑터를 보호).
  - SPEC-SEARCH-V6-001 — v6 RPC 가 distance tie 로 동일 id 를 두 번 반환할 가능성이 dedup 가드의 한 동기.
  - SPEC-AGENT-V2-REACT — `app/agents/react_loop.py` 의 시스템 프롬프트 조립 패턴 (`_SYSTEM_PROMPT + _PROACTIVE_DIRECTIVE + memory_context`). 본 SPEC 은 그 끝에 한 줄 append.
  - SPEC-CONVERSATION-LOG-001 — `card_sent` 이벤트 카운트가 dedup 후 값과 일치해야 함 (analytics 정합성). 본 SPEC 는 카드 발송 site 의 카운트만 줄일 뿐 timeline 의미는 보존.
- **Triggers / unblocks**:
  - Beta sprint follow-ups: #2 카테고리 미스매치 진단 SPEC, #4 맥락 혼동 SPEC, ★취향→랭킹 SPEC, ★Vision prompt SPEC — 모두 별도 사이클.
- **Affected modules in kikoai/ai**:
  - MODIFIED: `app/services/diversify_service.py`, `app/agents/react_loop.py`, `app/channels/adapter.py`, `app/channels/telegram/adapter.py`, `app/graphs/nodes/vision.py` (REQ-UX-004), `app/graphs/nodes/pinterest_ingest.py` (REQ-UX-004), `app/agents/tools/search_products.py` (REQ-UX-004 — both `search_products` and `refine_search` dispatch), `app/agents/tools/analyze_image.py` (REQ-UX-004).
  - NEW: `app/channels/pre_messages.py` (REQ-UX-004 — single-source `PRE_MESSAGES` dict, KO/EN, 4 keys).
  - NEW (tests): `tests/test_diversify/test_diversify_dedup.py`, `tests/test_agents/test_react_loop_lang_directive.py`, `tests/test_channels/test_telegram_chat_action.py`, `tests/test_agents/test_react_loop_typing_hook.py`, `tests/test_channels/test_pre_messages.py` (REQ-UX-004 dict snapshot + lang switch), `tests/test_graphs/test_pre_messages_nodes.py` (REQ-UX-004 vision/pinterest node firing), `tests/test_agents/test_pre_messages_tools.py` (REQ-UX-004 search/refine/analyze_image dispatch firing + idempotency + ordering vs typing).
  - UNCHANGED (asserted): `app/channels/lang.py`, `app/channels/persona.py`, `app/services/{search_service,embed_service,database_service}.py`, `app/infrastructure/repositories/search_repository.py`, all OTHER graph nodes under `app/graphs/nodes/` (e.g., `ingest.py`, `pick_item.py`, `ask_clarify.py`, `onboard_*.py` — REQ-UX-004 fires only at `vision_node` and `pinterest_ingest`).
- **Project context**: `/Users/hansangho/Desktop/kikoai/ai/CLAUDE.md` — update "핵심 파일" 표에 `app/channels/pre_messages.py` 신규 행 추가.
- **Research basis**: 베타 사용자 피드백 5종 관찰 (2026-05 비공식 채널). 빈도 + impact + 수정 surface 좁음의 합집합으로 3건 선정. v0.2.0 REQ-UX-004 는 typing indicator 도입(REQ-UX-003) 후 베타 사용자 추가 관찰 — "뭐가 진행 중인지" 인지 향상 요청.

---

## Definition of Done (P0)

- [ ] REQ-UX-001 implemented. `diversify_service` adds `seen_ids: set[str]` guard at loop entry. Candidates with truthy `id` already in `seen_ids` are dropped (`drops_dup += 1`). Falsy `id` (`None` / `""`) bypasses dedup. `[STEP 4.8]` log line includes `drops_dup=%d`. Existing brand/platform cap semantics byte-identical on unique-id inputs.
- [ ] REQ-UX-002 implemented. `run_react_loop` resolves `session_lang(sess)` and appends exactly one `[LANG=<ko|en> — MUST reply in <Korean|English>]` line as the LAST line of `system_content` (after `_SYSTEM_PROMPT`, `_PROACTIVE_DIRECTIVE`, and memory_context). Snapshot tests for both KO and EN pass. `respond` tool unchanged (already uses `session_lang(sess)`).
- [ ] REQ-UX-003 implemented. `MessengerAdapter` ABC exposes `async def send_chat_action(chat_id: int, action: str = "typing") -> bool` with default `return False`. `TelegramAdapter.send_chat_action` POSTs to `sendChatAction` endpoint with fail-open exception swallow (DEBUG log only). `run_react_loop` invokes `send_chat_action` exactly once via `asyncio.create_task` before `search_products` and `refine_search` tool dispatch, and exactly once before `respond` text send.
- [ ] REQ-UX-004 implemented. `app/channels/pre_messages.py` introduced with `PRE_MESSAGES` dict (4 keys × KO/EN). The 5 firing sites (`vision_node`, `search_products` dispatch, `refine_search` dispatch, `pinterest_ingest`, `analyze_image` dispatch) each invoke `adapter.send_text` with the matching `PRE_MESSAGES[key][lang]` before the underlying long-running operation. Idempotency marker (`_pre_msg_sent:{key}` on ctx/state) prevents re-send within the same turn. Pre-message fires BEFORE typing indicator (REQ-UX-003). Send failure is fail-open (DEBUG log only).
- [ ] All 7 new test files pass: `test_diversify_dedup.py`, `test_react_loop_lang_directive.py`, `test_telegram_chat_action.py`, `test_react_loop_typing_hook.py`, `test_pre_messages.py`, `test_pre_messages_nodes.py`, `test_pre_messages_tools.py`.
- [ ] AST-level test asserts `send_chat_action` is invoked ONLY at the 3 documented sites (search_products dispatch / refine_search dispatch / respond send) — no leakage to other tools.
- [ ] AST-level test asserts `PRE_MESSAGES` is imported ONLY by the 5 documented firing sites (`vision.py`, `search_products.py` × 2 dispatch branches, `pinterest_ingest.py`, `analyze_image.py`) — no leakage. (Test files importing for snapshot assertions are allow-listed.)
- [ ] All existing tests (`pytest -q` baseline) continue to pass under the same env (no flag-driven divergence — four changes are unconditional).
- [ ] `ruff check . && ruff format --check .` passes.
- [ ] An end-to-end manual test against the dev Telegram bot exercises:
      (a) Send a query that historically produced duplicates (or seed `state.raw_candidates` with a known-dup fixture); verify 0 duplicate `product_id` values in the resulting carousel.
      (b) Start a Korean conversation ("안녕"), then send a button-tap or short follow-up ("응"); verify bot reply is in Korean (no English drift).
      (c) Send a fresh photo for search; verify "kiko 입력 중…" indicator appears in the Telegram chat during the search → LLM phase.
      (d) Send a fresh photo (REQ-UX-004 Vision): verify the Korean pre-message "사진 잘 봤어요, 잠깐 분석해볼게요 👀" appears IMMEDIATELY (before typing indicator) and the Vision analysis follows.
      (e) Trigger a text-only search ("청바지 추천"): verify "잠시만요, 마음에 들 만한 거 찾아볼게요 🔍" appears before the carousel.
      (f) (If Pinterest bootstrap is enabled in dev) Send a Pinterest board URL during onboarding: verify "보드 살펴볼게요, 잠시만요 📌" appears before the scrape result.
- [ ] No new env var added. No feature flag introduced. (Consistent with SPEC-AGENT-V2-CLEANUP-001.)

---

## Implementation Plan Outline (informative — formalized in plan.md)

1. **Dedup guard** (`app/services/diversify_service.py`): add `seen_ids: set[str] = set()` and `drops_dup = 0` initialization. In the loop, after `c.get("id")` extraction: if truthy and in `seen_ids`, increment `drops_dup` and `continue`; else add to set on append. Extend `[STEP 4.8]` log line with `drops_dup`. Smoke test against existing diversify test suite.
2. **LANG directive** (`app/agents/react_loop.py`): import is already in scope or add `from app.channels.lang import session_lang`. After memory-context assembly (line ~417), compute `lang = session_lang(sess)` and append `f"\n\n[LANG={lang} — MUST reply in {LANG_NAME[lang]}]"` to `system_content`. Define a module-level `LANG_NAME = {"ko": "Korean", "en": "English"}`.
3. **Adapter ABC** (`app/channels/adapter.py`): add `async def send_chat_action(chat_id: int, action: str = "typing") -> bool: return False` as default. Keep it as a regular method (not `@abstractmethod`) so existing adapter implementations don't need to be touched.
4. **Telegram implementation** (`app/channels/telegram/adapter.py`): add `async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool` that POSTs to the existing Telegram endpoint with the existing httpx client. Swallow all exceptions, log at DEBUG. Return `bool` indicating success.
5. **Tool dispatch hook** (`app/agents/react_loop.py`): at the `search_products` / `refine_search` tool dispatch branch (existing site), add `asyncio.create_task(adapter.send_chat_action(chat_id))`. At the `respond` tool dispatch (before text send), do the same.
6. **Pre-messages module** (`app/channels/pre_messages.py` NEW, REQ-UX-004): define `PRE_MESSAGES: dict[str, dict[str, str]]` constant with 4 keys (`vision`, `search`, `pinterest`, `analyze_image`) × 2 langs (`ko`, `en`). Add a small helper `async def fire_pre_message(adapter, ctx_or_state, key: str, lang: str) -> None` that resolves the idempotency marker, sends `adapter.send_text(PRE_MESSAGES[key][lang])` awaited (ordering invariant), marks the marker, and swallows exceptions at DEBUG.
7. **Pre-message firing sites** (REQ-UX-004, 5 sites): in `app/graphs/nodes/vision.py` add `await fire_pre_message(..., "vision", lang)` at node entry; in `app/agents/tools/search_products.py` add `await fire_pre_message(..., "search", lang)` at both `search_products` dispatch entry and `refine_search` dispatch entry; in `app/graphs/nodes/pinterest_ingest.py` add `await fire_pre_message(..., "pinterest", lang)` at node entry; in `app/agents/tools/analyze_image.py` add `await fire_pre_message(..., "analyze_image", lang)` at dispatch entry. In each tool case, pre-message MUST fire BEFORE the REQ-UX-003 typing-indicator `_fire_typing` call (ordering invariant).
8. **Tests**: 7 new test files (4 from v0.1.0 + 3 from REQ-UX-004). Test for pre-messages dict uses dict-shape snapshot; test for graph node firing uses spy adapter + state; test for tool firing uses spy adapter + ctx + idempotency + ordering-vs-typing.
9. **Manual smoke** on dev bot: scenarios (a)/(b)/(c)/(d)/(e)/(f) above.

---

## Test Plan Outline (informative — formalized in acceptance.md)

- **Unit (`tests/test_diversify/test_diversify_dedup.py`)**: dedup-by-id positive case (2 duplicate ids in 5 candidates → 4 out); missing-id graceful bypass (`None` / `""` not collapsed); byte-identical regression on unique-id input; `drops_dup` counter and log line.
- **Unit (`tests/test_agents/test_react_loop_lang_directive.py`)**: KO snapshot — `system_content` ends with `[LANG=ko — MUST reply in Korean]`; EN snapshot — `[LANG=en — MUST reply in English]`; directive ordering (after memory_context); sticky validation across two turns where second turn is a button-tap with empty text.
- **Unit (`tests/test_channels/test_telegram_chat_action.py`)**: ABC default returns False (no raise); Telegram POST endpoint + payload verified via httpx MockTransport; fail-open behavior on HTTP 500 / timeout / generic HTTPError (each returns False, exactly one DEBUG log).
- **Unit (`tests/test_agents/test_react_loop_typing_hook.py`)**: spy adapter asserts exactly one `send_chat_action` call before `search_products` dispatch; same before `refine_search`; same before `respond` send; AST test asserts no other tool path invokes the helper; fire-and-forget timing (tool body completes in body-only time).
- **Unit (`tests/test_channels/test_pre_messages.py`)**: `PRE_MESSAGES` dict shape snapshot (exactly 4 keys × KO/EN, all non-empty); parametric KO vs EN lookup; AST/import scan asserts `PRE_MESSAGES` is imported only at the 5 documented sites + the test files.
- **Unit (`tests/test_graphs/test_pre_messages_nodes.py`)**: `vision_node` entry → `adapter.send_text` called once with `PRE_MESSAGES["vision"][lang]` before LiteLLM Vision call; `pinterest_ingest` entry → same with `PRE_MESSAGES["pinterest"][lang]` before Apify scrape; idempotency on state marker; fail-open on `send_text` raise.
- **Unit (`tests/test_agents/test_pre_messages_tools.py`)**: `search_products` dispatch → `send_text` called once with `PRE_MESSAGES["search"][lang]`; `refine_search` dispatch → same message (shared key); `analyze_image` dispatch → `PRE_MESSAGES["analyze_image"][lang]`; idempotency on ctx marker (second dispatch in same ctx does NOT re-send); ordering invariant (`send_text` BEFORE `send_chat_action` within the same dispatch); fail-open on `send_text` raise (underlying tool still executes); parametric KO/EN per site.
- **Regression**: full existing `tests/` tree green; in particular `tests/test_diversify/` byte-identical for unique-id inputs.
- **End-to-end manual**: the 6 scenarios (a-f) in the Definition of Done section against dev bot.
