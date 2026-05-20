---
id: SPEC-AGENT-UX-P0-001
plan_version: 0.2.0
spec_version: 0.2.0
created: 2026-05-20
methodology: DDD (ANALYZE-PRESERVE-IMPROVE)
target_branch: feature/agent-ux-p0
---

# Implementation Plan — SPEC-AGENT-UX-P0-001 v0.2.0

> **Plan HISTORY**:
> - 2026-05-20 (v0.2.0): REQ-UX-004 (사전 안내 멘트) plan 추가. 신규 단일-소스 모듈 `app/channels/pre_messages.py` + 5개 firing 지점 (Vision 노드 / search_products tool / refine_search tool / Pinterest 노드 / analyze_image tool). Idempotency 마커는 ctx (tools) / state (graph nodes) 에 `_pre_msg_sent:{key}` 키로 저장 — OQ-7 해소. `await adapter.send_text(...)` 동기 호출로 ordering 보장(OQ-8 해소) — REQ-UX-003 typing 직전. 단일 helper `fire_pre_message` 로 tools / nodes 양쪽 공통 호출(OQ-9 해소). 새 test 3 파일 (`test_pre_messages.py`, `test_pre_messages_nodes.py`, `test_pre_messages_tools.py`) 추가.
> - 2026-05-20 (v0.1.0): 초안 — 3 REQ(dedup / LANG directive / typing indicator) plan.
>
> **Scope guard**: WHAT/WHY는 spec.md에서 잠긴 상태. plan.md는 **HOW**만 결정한다. SPEC 에 잠긴 4 REQ(REQ-UX-001 dedup, REQ-UX-002 LANG directive, REQ-UX-003 typing indicator, REQ-UX-004 pre-action messages) 가 본 plan 의 입력. 본 plan 이 추가로 잠그는 결정은 SPEC §Open Questions (OQ-1 ~ OQ-9) 해소만이다.

> **Methodology**: **DDD (ANALYZE-PRESERVE-IMPROVE)**. 네 변경 모두 *기존 동작* 표면을 건드린다 — `diversify_service` 의 cap loop, `react_loop.py` 의 system prompt 조립, `TelegramAdapter` 의 send 패턴, 그리고 REQ-UX-004 는 Vision/Pinterest 그래프 노드 + search/refine/analyze_image tool dispatch 의 진입 분기. 각 변경 전에 현 동작을 capture 하는 characterization test 또는 snapshot 을 먼저 박고, byte-identical 보존을 PRESERVE 단계에서 보장한 뒤 IMPROVE 로 가드/directive/메서드/pre-message helper 를 추가한다. REQ-UX-004 의 `app/channels/pre_messages.py` 모듈은 NEW 파일이지만, **사용 site 마다 기존 동작 (Vision call / search dispatch / scrape) 의 시작 시점은 byte-identical 로 보존**된다 — pre-message 가 await 으로 그 직전에 한 줄 추가될 뿐.

> **HARD prerequisite**: 본 plan 은 어떤 새 env var / migration / 외부 의존도 추가하지 않는다. dev bot 운영 환경 (`AGENT_LLM_MODEL=claude-haiku-4-5` 등) 그대로.

---

## 0. Assumption Audit

| # | Assumption | Confidence | Risk if wrong |
|---|---|---|---|
| A1 | `state.raw_candidates` 의 각 dict 는 `id: str` 키를 갖는다 (v6 RPC 응답 포맷 SPEC-SEARCH-V6-001 contract). | High (search_repository contract + 코드 직접 확인) | id 가 없는 candidate 가 정상 케이스로 들어오면 dedup 가드는 fail-open 으로 통과 — 동작에 영향 없음. |
| A2 | `app/channels/lang.py::session_lang(sess)` 는 `"ko"` 또는 `"en"` 중 하나만 반환 (Hangul 유무 기준). | High (lang.py 직접 확인) | 다른 코드(`zh`, `ja`)가 추가될 경우 `LANG_NAME` dict 가 KeyError — fallback 으로 `LANG_NAME.get(lang, "English")` 사용. |
| A3 | `TelegramAdapter` 는 기존에 httpx 비동기 client 를 보유하고 `sendMessage` 에서 재사용 중. | High (adapter.py 448 LOC 인스펙션 필요) | client property 가 없으면 `plan.md` §4 에서 별도 helper 패턴 채택 — 단, 새 client 생성 금지. |
| A4 | ReAct loop 의 tool dispatch 는 단일 진입점에서 분기 (`_dispatch_tool` 또는 동치) — search/refine/respond hook 을 한 곳에서 추가 가능. | Medium (react_loop.py 775 LOC, dispatch helper 위치 확인 필요) | 분기점이 분산되어 있으면 3곳에 hook 분산 — AST test 가 site 수를 검증하므로 누락 시 fail. |
| A5 | `MessengerAdapter` ABC 에 새 default method 추가 시 모든 기존 어댑터 구현(현재 Telegram only)이 자동 inherit. | High (ABC 표준 동작) | None. |
| A6 | 기존 diversify 테스트 (`tests/test_diversify/` 또는 동치) 는 unique-id input 만 사용 — dedup 가드 추가로 byte-identical 보존됨. | Medium (테스트 디렉토리 확인 필요) | 기존 테스트가 dup-id input 을 사용한다면 expectation 갱신 필요 — characterization 단계에서 검출. |
| A7 | `respond` tool 의 `send_hybrid_batch` 호출 직전이 typing-indicator 1회 호출의 적합 지점. | Medium (respond tool 코드 확인 필요) | 만약 respond 내부에서 여러 send 가 분기된다면 첫 send 직전 1회만 — plan.md §5 가 결정. |
| A8 | (REQ-UX-004) `app/agents/tools/` 디렉토리에 `search_products.py`, `refine_search.py`(또는 `search_products.py` 안 dispatch 분기), `analyze_image.py` 가 각각 별도 tool wrapper 파일로 존재. CLAUDE.md "8-tool REGISTRY" 항목 확인됨. | High (CLAUDE.md `app/agents/tools/` 항목 직접 확인) | 만약 `refine_search` 가 `search_products.py` 안의 dispatch 분기로만 존재한다면 (가능성 있음 — UX 행렬에 B/C 모두 `app/agents/tools/search_products.py` 표기) — pre-message hook 은 두 분기 진입부 각각 1회 호출 (단일 파일, 두 site). |
| A9 | (REQ-UX-004) `MessengerAdapter` ABC 에 이미 `send_text` (또는 동치 `send_message` text-only 변종) 메서드가 존재. CLAUDE.md "send_text_with_buttons" 언급으로 미루어 `send_text` 계열 메서드 보유 추정. | Medium (adapter ABC 인스펙션 필요) | 만약 정확한 시그니처가 `send_text` 가 아니라면 (예: `send_message` + parse_mode 파라미터) — pre-message helper 는 그 시그니처에 맞춰 호출. SPEC 은 "어떤 텍스트 전송 메서드" 만 lock, 메서드명은 plan 이 결정 가능. |
| A10 | (REQ-UX-004) Graph 노드 (`vision_node`, `pinterest_ingest`) 에서 adapter 와 state 에 접근 가능 (LangGraph context 또는 module-level adapter ctx). | High (CLAUDE.md "send_results 노드는 그래프 미등록… `send_hybrid_batch` 성공 지점" 등 노드에서 adapter 사용 패턴 확인됨) | 만약 노드에서 adapter 접근이 직접 안 되면, state 에서 chat_id 만 꺼내 별도 adapter ctx helper 로 조회 — react_loop 의 `_adapter_ctx.get_adapter()` 패턴 그대로 차용. |

**Critical surfacing**: A3 (httpx client 재사용 패턴), A4 (dispatch helper 위치), A8 (tool 파일 분리/통합 여부), A9 (`send_text` 메서드 시그니처) 는 코드 시작 전 1차 인스펙션 필요. 나머지는 unit-level characterization 으로 검증.

---

## 1. Module Structure & Public Surface

### 1.1 `app/services/diversify_service.py` 변경 (REQ-UX-001)

Diff scope: 캡 loop 진입부 + log line 한 곳.

```python
# 기존 imports / signature 무변경.
async def diversify_service(state: PipelineState) -> PipelineState:
    state.start("diversify")
    req = state.request
    target = req.final_limit or tolerance_to_target_count(req.tolerance)
    brand_cap = settings.SEARCH_BRAND_CAP * 3 if req.brand_filter else settings.SEARCH_BRAND_CAP
    platform_cap = settings.SEARCH_PLATFORM_CAP

    logger.info(
        "[STEP 4.7][diversify] 시작 — input=%d target=%d brand_cap=%d platform_cap=%d tolerance=%.2f",
        len(state.raw_candidates), target, brand_cap, platform_cap, req.tolerance,
    )

    seen_brand: dict[str, int] = {}
    seen_platform: dict[str, int] = {}
    seen_ids: set[str] = set()                            # NEW (REQ-UX-001)
    out: list[dict] = []
    drops_brand = 0
    drops_platform = 0
    drops_dup = 0                                          # NEW (REQ-UX-001)

    for c in state.raw_candidates:
        pid = c.get("id")                                  # NEW
        if pid and pid in seen_ids:                        # NEW — falsy bypass
            drops_dup += 1
            continue
        brand = (c.get("brand") or "").lower()
        platform = (c.get("platform") or "").lower()
        if seen_brand.get(brand, 0) >= brand_cap:
            drops_brand += 1
            continue
        if seen_platform.get(platform, 0) >= platform_cap:
            drops_platform += 1
            continue
        out.append(c)
        if pid:                                            # NEW
            seen_ids.add(pid)
        seen_brand[brand] = seen_brand.get(brand, 0) + 1
        seen_platform[platform] = seen_platform.get(platform, 0) + 1
        if len(out) >= target:
            break

    brand_dist = sorted(seen_brand.items(), key=lambda x: -x[1])[:5]
    platform_dist = sorted(seen_platform.items(), key=lambda x: -x[1])[:5]
    logger.info(
        "[STEP 4.8][diversify] 끝 — out=%d drops_brand=%d drops_platform=%d drops_dup=%d",   # MODIFIED
        len(out), drops_brand, drops_platform, drops_dup,
    )
    logger.info("[STEP 4.8][diversify] brand_top5=%s", brand_dist)
    logger.info("[STEP 4.8][diversify] platform_top5=%s", platform_dist)

    state.final_candidates = out
    state.counts["after_diversify"] = len(out)
    state.counts["final"] = len(out)
    state.end("diversify")
    return state
```

**Resolved OQ-1** (dedup key extraction): `c.get("id")` 사용 (단순, dict.get). 별도 helper 함수 도입하지 않음.

**Resolved OQ-6** (log line 포맷): 기존 `out=%d drops_brand=%d drops_platform=%d` 끝에 `drops_dup=%d` trailing append. 위치 변경 없이 길이만 +1.

### 1.2 `app/agents/react_loop.py` 변경 (REQ-UX-002 + REQ-UX-003)

#### LANG directive (REQ-UX-002)

```python
# 모듈 수준 상수 추가
LANG_NAME: dict[str, str] = {"ko": "Korean", "en": "English"}

# 기존 import 옆에 추가
from app.channels.lang import session_lang

# system_content 조립 (기존 line 411~417 직후)
system_content = f"{_SYSTEM_PROMPT}\n\n{_PROACTIVE_DIRECTIVE}"
# ... memory_context 주입 (기존) ...
system_content = f"{system_content}\n\n{mem_block}"
# NEW — LANG directive (memory_context 다음, user message 펜스 직전)
lang = session_lang(sess)
lang_label = LANG_NAME.get(lang, "English")
system_content = f"{system_content}\n\n[LANG={lang} — MUST reply in {lang_label}]"
```

**Resolved OQ-2** (directive 위치): memory_context 다음 라인. user message 펜스 직전이 transformer 최근접성 측면에서 가장 강함.

#### Typing indicator hook (REQ-UX-003)

```python
# 모듈 수준 import 추가
import asyncio
# from app.channels.factory import get_adapter  # 또는 _adapter_ctx 헬퍼

# tool dispatch helper 내부 (search_products / refine_search / respond 분기)
async def _dispatch_tool(tool_name: str, args: dict, ctx: dict) -> dict:
    # NEW: typing indicator hook (REQ-UX-003)
    if tool_name in ("search_products", "refine_search"):
        _fire_typing(ctx)
    elif tool_name == "respond":
        _fire_typing(ctx)
    # ... 기존 dispatch ...


def _fire_typing(ctx: dict) -> None:
    """Fire-and-forget typing indicator. Never raises (REQ-UX-003 fail-open)."""
    try:
        adapter = _adapter_ctx.get_adapter()  # 기존 헬퍼 재사용
        chat_id = ctx.get("chat_id")
        if adapter is None or chat_id is None:
            return
        asyncio.create_task(adapter.send_chat_action(chat_id, "typing"))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("typing indicator skipped: %r", exc)
```

**Resolved OQ-3** (hook 위치): `_dispatch_tool` 진입 분기에서 3개 tool name 매칭 (`search_products`, `refine_search`, `respond`). 호출은 helper `_fire_typing` 1곳으로 단일 소스 — AST test 가 helper 의 caller 가 dispatch 분기 1곳임을 검증.

### 1.3 `app/channels/adapter.py` 변경 (REQ-UX-003 ABC)

```python
# 기존 ABC 끝에 추가 (74 LOC → 78 LOC 예상)
class MessengerAdapter(ABC):
    # ... 기존 abstract methods ...

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        """Optional typing indicator. Default no-op (returns False).

        Concrete adapters MAY override (currently Telegram only — SPEC-AGENT-UX-P0-001 REQ-UX-003).
        """
        return False
```

**Resolved OQ-5** (다른 어댑터 default): `return False` (no-op). 다른 채널 어댑터가 명시적 override 없이도 자동 skip. `@abstractmethod` 사용 안 함 — 기존 어댑터 구현체 무수정 보장.

### 1.4 `app/channels/telegram/adapter.py` 변경 (REQ-UX-003 구현)

```python
# 기존 TelegramAdapter 클래스 안에 추가
async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
    """POST sendChatAction. Fail-open (REQ-UX-003)."""
    url = f"https://api.telegram.org/bot{self._token}/sendChatAction"
    payload = {"chat_id": chat_id, "action": action}
    try:
        resp = await self._client.post(url, json=payload, timeout=2.0)
        if resp.status_code != 200:
            logger.debug("sendChatAction non-200: status=%d body=%s", resp.status_code, resp.text[:200])
            return False
        body = resp.json()
        return bool(body.get("ok"))
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("sendChatAction failed: %r", exc)
        return False
```

**Resolved OQ-4** (httpx client 재사용): 기존 `self._client` (sendMessage 가 쓰는 동일 client) 사용. 새 client 생성 금지 — 별도 `httpx.AsyncClient()` instantiation 금지 by AST test.

Timeout 2.0s 고정 — typing indicator 는 빠른 신호이므로 긴 timeout 의미 없음.

### 1.5 `app/channels/pre_messages.py` 신규 (REQ-UX-004 단일 소스)

```python
"""Pre-action messages — single source for fixed user-facing intent signals.

SPEC-AGENT-UX-P0-001 v0.2.0 REQ-UX-004.

각 키는 1~2초 이상 걸리는 작업 직전 보내는 의도-안내 멘트.
드리프트는 SPEC-level 변경 — snapshot test 가 키 셋트와 값 비-빈 여부를 잠근다.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Single source of truth — KO/EN fixed wording.
PRE_MESSAGES: dict[str, dict[str, str]] = {
    "vision":        {"ko": "사진 잘 봤어요, 잠깐 분석해볼게요 👀",   "en": "Got it! Let me take a closer look 👀"},
    "search":        {"ko": "잠시만요, 마음에 들 만한 거 찾아볼게요 🔍", "en": "One sec — let me find something you'll love 🔍"},
    "pinterest":     {"ko": "보드 살펴볼게요, 잠시만요 📌",            "en": "Checking out your board, hang tight 📌"},
    "analyze_image": {"ko": "이미지 들여다볼게요 👀",                 "en": "Looking at this image 👀"},
}


def _marker_key(key: str) -> str:
    """Idempotency marker key for ctx / state."""
    return f"_pre_msg_sent:{key}"


async def fire_pre_message(
    adapter: Any,
    ctx_or_state: dict[str, Any],
    *,
    key: str,
    lang: str,
    chat_id: int | None = None,
) -> None:
    """Send a fixed pre-action message — idempotent per turn, fail-open.

    - `key`: one of `PRE_MESSAGES` keys (`vision` / `search` / `pinterest` / `analyze_image`).
    - `lang`: `"ko"` or `"en"`. Falls back to `"en"` for any other value.
    - `ctx_or_state`: dict carrying the idempotency marker. Mutated on success.
    - `chat_id`: resolved by caller (from session / state).

    NEVER raises. send_text failures emit a single DEBUG log line.
    """
    marker = _marker_key(key)
    if ctx_or_state.get(marker):
        return
    text_map = PRE_MESSAGES.get(key)
    if not text_map:
        logger.debug("pre_message: unknown key=%r — skipped", key)
        return
    text = text_map.get(lang) or text_map["en"]
    if not chat_id:
        return
    try:
        await adapter.send_text(chat_id, text)
        ctx_or_state[marker] = True
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("pre_message send failed key=%s lang=%s: %r", key, lang, exc)
```

**Resolved OQ-7** (marker 저장 위치): tools 는 `ctx` dict 사용, graph nodes 는 `state.__dict__` 또는 동치 dict-like 인터페이스. helper 가 `dict[str, Any]` 만 받으므로 양쪽 모두 동치.

**Resolved OQ-8** (sync vs async firing): `await adapter.send_text(...)` 동기 호출. 비용 ~100-300ms 수용 — ordering 보장이 우선 (pre-msg → typing → 본 작업). 비동기 전환은 R10 follow-up.

**Resolved OQ-9** (helper 분리/통합): 단일 helper `fire_pre_message` 가 tools 와 nodes 양쪽에서 호출. AST test 가 import 사이트를 5개로 잠금.

### 1.6 REQ-UX-004 firing site 변경 (5개)

#### Site A — `app/graphs/nodes/vision.py` 노드 진입부

```python
# 노드 함수 본문 첫 부분 (Vision LiteLLM 호출 직전)
from app.channels.pre_messages import fire_pre_message
from app.channels.lang import session_lang

async def vision_node(state: WorkingState) -> ...:
    sess = state.session  # 혹은 동치 접근
    lang = session_lang(sess)
    adapter = _adapter_ctx.get_adapter()
    chat_id = getattr(sess, "chat_id", None) or state.chat_id
    # NEW (REQ-UX-004) — pre-message BEFORE typing indicator AND before LiteLLM call
    await fire_pre_message(adapter, state.__dict__, key="vision", lang=lang, chat_id=chat_id)
    # ... 기존 typing indicator 가 있다면 그 다음 ...
    # ... 기존 Vision LiteLLM 호출 ...
```

#### Site B/C — `app/agents/tools/search_products.py` dispatch 진입부 (search + refine 양쪽)

```python
# search_products tool wrapper 진입부
async def dispatch(args, ctx):
    lang = ctx.get("lang") or "en"
    adapter = ctx.get("adapter") or _adapter_ctx.get_adapter()
    chat_id = ctx.get("chat_id")
    # NEW (REQ-UX-004) — pre-message FIRST
    await fire_pre_message(adapter, ctx, key="search", lang=lang, chat_id=chat_id)
    # THEN REQ-UX-003 typing
    _fire_typing(ctx)
    # ... 기존 run_image_search / run_text_only_search ...

# refine_search dispatch 진입부 — 같은 key="search"
async def dispatch_refine(args, ctx):
    lang = ctx.get("lang") or "en"
    adapter = ctx.get("adapter") or _adapter_ctx.get_adapter()
    chat_id = ctx.get("chat_id")
    await fire_pre_message(adapter, ctx, key="search", lang=lang, chat_id=chat_id)  # 같은 메시지
    _fire_typing(ctx)
    # ... 기존 refine 실행 ...
```

#### Site D — `app/graphs/nodes/pinterest_ingest.py` 노드 진입부

```python
async def pinterest_ingest(state: WorkingState) -> ...:
    sess = state.session
    lang = session_lang(sess)
    adapter = _adapter_ctx.get_adapter()
    chat_id = getattr(sess, "chat_id", None) or state.chat_id
    await fire_pre_message(adapter, state.__dict__, key="pinterest", lang=lang, chat_id=chat_id)
    # ... 기존 Apify 호출 ...
```

#### Site E — `app/agents/tools/analyze_image.py` dispatch 진입부

```python
async def dispatch(args, ctx):
    lang = ctx.get("lang") or "en"
    adapter = ctx.get("adapter") or _adapter_ctx.get_adapter()
    chat_id = ctx.get("chat_id")
    await fire_pre_message(adapter, ctx, key="analyze_image", lang=lang, chat_id=chat_id)
    # (typing indicator: analyze_image 는 search 와 다르게 REQ-UX-003 에 미포함. SPEC 의 typing 정책은 search/refine/respond 3 지점만. pre-message 는 5 지점 — analyze_image 는 pre-message ONLY.)
    # ... 기존 LiteLLM Vision 호출 ...
```

**Ordering invariant (테스트로 잠금)**: site B/C 의 경우 `await send_text` → `_fire_typing` (create_task) → 본 작업. site A/D/E 는 typing 미포함 — `await send_text` → 본 작업.

**Idempotency**: 같은 ctx (tools) 또는 같은 state instance (nodes) 안에서 helper 가 두 번 불려도 marker 가 두 번째 호출을 차단.

---

## 2. Test Strategy

### 2.1 `tests/test_diversify/test_diversify_dedup.py` (NEW)

```python
# 4 cases
async def test_dedup_drops_duplicate_id():
    # 5 candidates, 2 share id="prod-A"
    state = _make_state([
        {"id": "prod-A", "brand": "b1", "platform": "p1"},
        {"id": "prod-B", "brand": "b2", "platform": "p2"},
        {"id": "prod-A", "brand": "b3", "platform": "p3"},  # dup
        {"id": "prod-C", "brand": "b4", "platform": "p4"},
        {"id": "prod-D", "brand": "b5", "platform": "p5"},
    ])
    out = await diversify_service(state)
    ids = [c["id"] for c in out.final_candidates]
    assert len(set(ids)) == len(ids)
    assert "prod-A" in ids and ids.count("prod-A") == 1
    assert len(ids) == 4

async def test_missing_id_bypass_dedup():
    # id=None / id="" 는 dedup 우회
    state = _make_state([
        {"id": None, "brand": "b1", "platform": "p1"},
        {"id": "",   "brand": "b2", "platform": "p2"},
        {"id": "prod-X", "brand": "b3", "platform": "p3"},
    ])
    out = await diversify_service(state)
    assert len(out.final_candidates) >= 2  # 누락 id 들 collapse 되지 않음 (brand/platform cap subject)

async def test_byte_identical_on_unique_ids(caplog):
    # unique id input — pre-SPEC 결과와 동일해야 함
    ...

async def test_drops_dup_in_log_line(caplog):
    state = _make_state([{"id": "p1", ...}, {"id": "p1", ...}])
    await diversify_service(state)
    assert "drops_dup=1" in caplog.text
```

### 2.2 `tests/test_agents/test_react_loop_lang_directive.py` (NEW)

```python
# 4 cases
def test_system_prompt_ko_directive_last_line():
    sess = _make_session(lang="ko")
    system_content = _assemble_system_content(state, sess)  # 테스트 헬퍼 — react_loop 내부 함수 호출
    lines = system_content.split("\n")
    assert lines[-1] == "[LANG=ko — MUST reply in Korean]"

def test_system_prompt_en_directive_last_line():
    sess = _make_session(lang="en")
    system_content = _assemble_system_content(state, sess)
    lines = system_content.split("\n")
    assert lines[-1] == "[LANG=en — MUST reply in English]"

def test_system_prompt_persona_and_memory_unchanged():
    # _SYSTEM_PROMPT / _PROACTIVE_DIRECTIVE / mem_block 본문은 변하지 않음
    ...

async def test_sticky_lang_across_button_tap_turn():
    # 1턴: 한글 "안녕" → session.lang = "ko"
    # 2턴: button-tap callback, text 비어 있음 → directive 여전히 KO
    ...
```

테스트 셋업: `react_loop.py` 의 system_content 조립 path 를 단위 함수로 추출하거나 (`_assemble_system_content` helper), 또는 LLM client 를 mock 해서 `bind_tools` 후 첫 호출의 messages[0]["content"] 를 캡처. 후자 권장 (private API 노출 회피).

### 2.3 `tests/test_channels/test_telegram_chat_action.py` (NEW)

```python
# 4 cases — httpx MockTransport / respx 사용
async def test_send_chat_action_posts_to_telegram(respx_mock):
    route = respx_mock.post("https://api.telegram.org/bot<TOKEN>/sendChatAction").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    adapter = TelegramAdapter(token="<TOKEN>", ...)
    ok = await adapter.send_chat_action(chat_id=42)
    assert ok is True
    assert route.called
    sent_body = json.loads(route.calls[0].request.content)
    assert sent_body == {"chat_id": 42, "action": "typing"}

async def test_send_chat_action_fail_open_on_http_500(respx_mock, caplog):
    respx_mock.post(...).mock(return_value=httpx.Response(500))
    ok = await TelegramAdapter(...).send_chat_action(42)
    assert ok is False
    debug_lines = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(debug_lines) == 1
    assert all(r.levelname not in ("WARNING", "ERROR") for r in caplog.records)

async def test_send_chat_action_fail_open_on_timeout(respx_mock):
    respx_mock.post(...).mock(side_effect=httpx.TimeoutException("timeout"))
    ok = await TelegramAdapter(...).send_chat_action(42)
    assert ok is False

def test_abc_default_returns_false():
    class FakeAdapter(MessengerAdapter):
        # 기존 abstract methods 만 구현, send_chat_action override 안 함
        ...
    fa = FakeAdapter(...)
    assert asyncio.run(fa.send_chat_action(42)) is False
```

### 2.4 `tests/test_agents/test_react_loop_typing_hook.py` (NEW)

```python
# 4 cases
class _SpyAdapter(MessengerAdapter):
    def __init__(self):
        self.chat_action_calls: list[tuple[int, str]] = []
    async def send_chat_action(self, chat_id, action="typing"):
        self.chat_action_calls.append((chat_id, action))
        return True
    # 기타 abstract methods 는 no-op

async def test_typing_fired_before_search_products():
    spy = _SpyAdapter()
    _adapter_ctx.set_adapter(spy)
    await _dispatch_tool("search_products", {...}, ctx={"chat_id": 42})
    await asyncio.sleep(0)  # create_task 처리 yield
    assert len(spy.chat_action_calls) == 1
    assert spy.chat_action_calls[0] == (42, "typing")

async def test_typing_fired_before_refine_search():
    ...  # 동일 패턴

async def test_typing_fired_before_respond():
    ...

def test_typing_not_fired_for_other_tools(...):
    # ast scan: send_chat_action 호출이 _fire_typing 안에만 존재
    # 그리고 _fire_typing 호출은 _dispatch_tool 안의 3개 tool name 분기에만 존재
    src = inspect.getsource(react_loop)
    tree = ast.parse(src)
    typing_callers = [...]
    assert len(typing_callers) <= ... # exact site count
```

### 2.5 `tests/test_channels/test_pre_messages.py` (NEW, REQ-UX-004)

```python
# 4 cases — 단순 dict + import 스캔
from app.channels.pre_messages import PRE_MESSAGES, fire_pre_message

def test_pre_messages_shape_snapshot():
    # 정확히 4개 키, 각 키마다 ko/en 둘 다 비어있지 않은 문자열
    assert set(PRE_MESSAGES.keys()) == {"vision", "search", "pinterest", "analyze_image"}
    for key, langs in PRE_MESSAGES.items():
        assert set(langs.keys()) == {"ko", "en"}
        assert langs["ko"].strip() and langs["en"].strip()

@pytest.mark.parametrize("key", ["vision", "search", "pinterest", "analyze_image"])
@pytest.mark.parametrize("lang", ["ko", "en"])
async def test_fire_pre_message_sends_correct_text(key, lang):
    spy = _SpyAdapter()
    ctx = {}
    await fire_pre_message(spy, ctx, key=key, lang=lang, chat_id=42)
    assert spy.text_calls == [(42, PRE_MESSAGES[key][lang])]
    assert ctx[f"_pre_msg_sent:{key}"] is True

async def test_fire_pre_message_idempotent():
    spy = _SpyAdapter()
    ctx = {}
    await fire_pre_message(spy, ctx, key="search", lang="ko", chat_id=42)
    await fire_pre_message(spy, ctx, key="search", lang="ko", chat_id=42)
    assert len(spy.text_calls) == 1  # 두 번째 호출은 marker 로 차단

async def test_fire_pre_message_fail_open_swallow(caplog):
    class _RaisingAdapter:
        async def send_text(self, chat_id, text):
            raise httpx.TimeoutException("boom")
    ctx = {}
    await fire_pre_message(_RaisingAdapter(), ctx, key="vision", lang="ko", chat_id=42)
    # marker 미설정 (재시도 가능), DEBUG 1개 로그, WARN/ERROR 없음
    assert ctx.get("_pre_msg_sent:vision") is None
    debug_lines = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert len(debug_lines) >= 1
    assert all(r.levelname not in ("WARNING", "ERROR") for r in caplog.records)

def test_pre_messages_import_sites_ast_scan():
    """REQ-UX-004: PRE_MESSAGES / fire_pre_message 임포트는 5개 site + 테스트만 허용."""
    import ast, pathlib
    allowed_runtime = {
        "app/graphs/nodes/vision.py",
        "app/agents/tools/search_products.py",  # search + refine 양쪽 (단일 파일)
        "app/graphs/nodes/pinterest_ingest.py",
        "app/agents/tools/analyze_image.py",
    }
    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for path in root.glob("app/**/*.py"):
        rel = str(path.relative_to(root))
        if rel == "app/channels/pre_messages.py":  # 자기 자신
            continue
        src = path.read_text(encoding="utf-8")
        if "pre_messages" not in src:
            continue
        if rel not in allowed_runtime:
            offenders.append(rel)
    assert offenders == [], f"Unauthorized pre_messages import: {offenders}"
```

### 2.6 `tests/test_graphs/test_pre_messages_nodes.py` (NEW, REQ-UX-004)

```python
# 6 cases — vision + pinterest 노드 × 정상/idempotency/fail-open
async def test_vision_node_fires_pre_message_ko():
    spy = _SpyAdapter()
    _adapter_ctx.set_adapter(spy)
    state = _make_state(lang="ko", chat_id=42, image_url="...")
    await vision_node(state)
    # 첫 send_text 가 PRE_MESSAGES["vision"]["ko"]
    assert spy.text_calls[0] == (42, PRE_MESSAGES["vision"]["ko"])
    # Vision LiteLLM 호출이 그 다음에 일어났는지는 mock 으로 확인 (call order)

async def test_vision_node_fires_pre_message_en():
    ...  # 동일, lang="en"

async def test_vision_node_idempotent_on_reentry():
    # 같은 state 객체로 노드 두 번 실행 → send_text 1회만
    ...

async def test_pinterest_ingest_fires_pre_message_ko():
    ...

async def test_pinterest_ingest_fires_pre_message_en():
    ...

async def test_pinterest_node_send_text_failure_does_not_block_apify():
    # send_text 가 raise 해도 Apify 호출은 그대로 실행
    ...
```

### 2.7 `tests/test_agents/test_pre_messages_tools.py` (NEW, REQ-UX-004)

```python
# 8 cases — search/refine/analyze_image × 정상/idempotency/ordering/fail-open
async def test_search_products_dispatch_fires_search_message_ko():
    spy = _SpyAdapter()
    ctx = {"adapter": spy, "lang": "ko", "chat_id": 42}
    await search_products_dispatch(args={...}, ctx=ctx)
    assert spy.text_calls[0] == (42, PRE_MESSAGES["search"]["ko"])

async def test_refine_search_dispatch_fires_same_search_message():
    # refine_search 도 key="search" 로 같은 메시지
    spy = _SpyAdapter()
    ctx = {"adapter": spy, "lang": "ko", "chat_id": 42}
    await refine_search_dispatch(args={...}, ctx=ctx)
    assert spy.text_calls[0] == (42, PRE_MESSAGES["search"]["ko"])

async def test_analyze_image_dispatch_fires_analyze_image_message_en():
    spy = _SpyAdapter()
    ctx = {"adapter": spy, "lang": "en", "chat_id": 42}
    await analyze_image_dispatch(args={...}, ctx=ctx)
    assert spy.text_calls[0] == (42, PRE_MESSAGES["analyze_image"]["en"])

async def test_tools_idempotent_within_same_ctx():
    spy = _SpyAdapter()
    ctx = {"adapter": spy, "lang": "ko", "chat_id": 42}
    await search_products_dispatch(args={...}, ctx=ctx)
    await search_products_dispatch(args={...}, ctx=ctx)
    # 두 번째 dispatch 는 marker 차단
    search_text_count = sum(1 for c in spy.text_calls if c[1] == PRE_MESSAGES["search"]["ko"])
    assert search_text_count == 1

async def test_ordering_pre_message_before_typing_indicator():
    spy = _SpyAdapter()  # send_text + send_chat_action 둘 다 시간순 기록
    ctx = {"adapter": spy, "lang": "ko", "chat_id": 42}
    await search_products_dispatch(args={...}, ctx=ctx)
    # spy.events: [("text", ...), ("chat_action", ...), ...]
    assert spy.events[0][0] == "text"
    assert spy.events[1][0] == "chat_action"

async def test_search_dispatch_send_text_failure_does_not_block_search():
    # send_text raise → search 본 작업 실행됨 (run_image_search 호출 mock 으로 확인)
    ...

async def test_analyze_image_dispatch_send_text_failure_does_not_block_vision():
    ...

@pytest.mark.parametrize("dispatch_fn,key", [
    (search_products_dispatch, "search"),
    (refine_search_dispatch, "search"),
    (analyze_image_dispatch, "analyze_image"),
])
async def test_each_dispatch_uses_session_lang(dispatch_fn, key):
    # 같은 ctx 에 lang="ko" / "en" 두 케이스 verify
    ...
```

---

## 3. Sequencing (Run Phase tasks)

| Task | Description | Acceptance |
|---|---|---|
| UX-T01 | Inspect `react_loop.py` dispatch helper location & httpx client pattern in `TelegramAdapter` & adapter `send_text` 시그니처 (A9) & graph nodes' adapter/state 접근 패턴 (A10) & `search_products` vs `refine_search` 파일 분리 여부 (A8). | 문서화 (`progress.md`). |
| UX-T02 | Characterization: capture current `[STEP 4.8]` log line + current `system_content` snapshot (KO/EN) + current TelegramAdapter sendMessage path + current `vision_node` / `pinterest_ingest` 첫줄 동작 + search/analyze tool dispatch 진입 동작. | Baseline tests committed (no behavior change). |
| UX-T03 | REQ-UX-001 implementation + 4 dedup tests. | `tests/test_diversify/test_diversify_dedup.py` green. |
| UX-T04 | REQ-UX-002 implementation + 4 LANG directive tests. | `tests/test_agents/test_react_loop_lang_directive.py` green. |
| UX-T05 | REQ-UX-003 ABC + Telegram impl + 4 chat_action tests. | `tests/test_channels/test_telegram_chat_action.py` green. |
| UX-T06 | REQ-UX-003 dispatch hook + 4 typing-hook tests + AST scan. | `tests/test_agents/test_react_loop_typing_hook.py` green. |
| UX-T07a | REQ-UX-004 single-source 모듈 `app/channels/pre_messages.py` 작성 + 4 dict/helper tests. | `tests/test_channels/test_pre_messages.py` green. |
| UX-T07b | REQ-UX-004 graph node firing (vision + pinterest) + 6 node tests. | `tests/test_graphs/test_pre_messages_nodes.py` green. |
| UX-T07c | REQ-UX-004 tool dispatch firing (search/refine/analyze_image) + 8 tool tests (ordering + idempotency + fail-open). | `tests/test_agents/test_pre_messages_tools.py` green. |
| UX-T08 | Full regression: `pytest -q`. | All existing tests pass. |
| UX-T09 | `ruff check . && ruff format --check .`. | Green. |
| UX-T10 | Manual smoke on dev bot (6 scenarios from SPEC §DoD: a-f). | Recorded in `progress.md`. |
| UX-T11 | acceptance.md final mapping update. | All P0 REQ rows show automated test paths. |
| UX-T12 | CLAUDE.md 핵심 파일 표에 `app/channels/pre_messages.py` 행 추가. | CLAUDE.md diff committed. |

Priority order: UX-T01 (inspection) → UX-T02 (baseline) → UX-T03~UX-T06 in parallel (independent surface) → UX-T07a (모듈 먼저) → UX-T07b/UX-T07c in parallel → UX-T08~UX-T12 sequential.

---

## 4. Risk Mitigation Details

### R1 — Dedup drops legitimate candidates

- Mitigation: falsy-id bypass (`if pid and pid in seen_ids`) + regression test `test_byte_identical_on_unique_ids` ensures pre-SPEC behavior preserved when no duplicates exist.
- Monitor: post-merge, `[STEP 4.8] drops_dup=N` log should be 0 on most turns. Sustained `drops_dup > 0` indicates upstream RPC drift — file separate ticket.

### R2 — LLM ignores LANG directive

- Mitigation: directive at LAST line (highest recency). Snapshot test brittleness intentional.
- Follow-up if observed: move directive into user-message fence (`_build_user_message`) — separate SPEC.

### R3 — `sendChatAction` rate limit

- Mitigation: per-turn cap is 2-3 calls (search/refine + respond). Telegram's per-chat per-second limit is ~1 — within bound for normal flow. 429 silently absorbed by fail-open.

### R4 — httpx client reuse

- Mitigation: reuse `self._client` (existing). AST test ban: `httpx.AsyncClient(` instantiation in `TelegramAdapter.send_chat_action`.

### R6 — Typing fires at unrelated callbacks

- Mitigation: `_fire_typing` 호출 site 가 `_dispatch_tool` 내부 단일 분기. AST test enumerates the 3 tool names allow-listed. 다른 tool 추가 시 명시적 SPEC change 필요.

### R9 — Pre-message noise (REQ-UX-004)

- Mitigation: idempotency marker (`_pre_msg_sent:{key}`) on ctx/state — 같은 turn 안에서 같은 key 두 번째 호출은 helper 가 차단. 5개 firing site 는 서로 다른 code path (Vision graph node vs analyze_image tool 은 한 turn 에 동시 일어나지 않음). `test_pre_messages_import_sites_ast_scan` 가 import 경로 5개로 잠금 — 새 site 추가 시 SPEC 변경 필요.

### R10 — Pre-message send latency (REQ-UX-004)

- Mitigation: `await adapter.send_text(...)` 동기 호출은 ~100-300ms 비용. ordering 보장이 우선이므로 SPEC 단계에서 동기 결정. P50 latency-to-first-card 모니터링 — 회귀 관측 시 helper 만 `asyncio.create_task` 로 전환 (follow-up SPEC).
- Telegram send_text 가 rate-limit 잡힐 경우 fail-open 으로 swallow — 사용자는 멘트만 못 보고 검색은 그대로 진행.

### R11 — Pre-message wording drift (REQ-UX-004)

- Mitigation: snapshot test (`test_pre_messages_shape_snapshot`) 가 4 키 × KO/EN 비-빈 문자열 잠금. 정확한 wording 은 dict literal 자체가 진실 — 변경 시 SPEC version bump 필요 (`spec.md` REQ-UX-004 의 표 + dict literal 둘 다 동기 갱신). PR review 에서 어느 한 쪽만 바뀌면 reject.

---

## 5. Cutover

1. Implement on `feature/agent-ux-p0` branch.
2. Local `pytest -q` green + manual smoke.
3. PR → review (focus: dedup graceful, LANG snapshot drift detection, typing fail-open, pre-message dict drift + 5-site AST scan + ordering invariant).
4. Merge to `dev`.
5. dev-ai redeploy (existing cutover playbook in `docs/infra/deployment.md`).
6. 24h observation: `docker logs ai-server | grep "drops_dup"` — confirm dedup activity; `grep "LANG=ko"` / `LANG=en` in system prompt logs (if Langfuse trace 로 캡처되면 거기서); `grep "pre_message send failed"` — fail-open 카운트 모니터링 (지속적이면 별도 ticket); 사용자 베타 채널에 "한글 답변 끊김" / "같은 옷 두 번" / "응답 신호 없음" / "뭐가 진행중인지 모름" 신고가 감소했는지 확인.

---

## 6. Out-of-Plan Items

- 별도 SPEC 후속(beta #2/#4/★rank/★vision) 은 본 plan 의 work breakdown 에 포함하지 않음.
- 새 env var / migration / 외부 서비스 의존 없음 — 본 plan 은 코드/테스트 변경만.
- Langfuse 새 span 추가 없음 — typing/dedup/pre-message 모두 기존 log 라인 + DEBUG 라인으로 충분.
- REQ-UX-004 `await` → `create_task` 전환 (R10 follow-up) — 본 plan 에 미포함. 별도 SPEC 또는 hotfix.
- Pre-message 다국어 확장 (`ja`/`zh`) — `app/channels/lang.py::detect_lang` 자체 확장이 선행되어야 하므로 별도 SPEC.
