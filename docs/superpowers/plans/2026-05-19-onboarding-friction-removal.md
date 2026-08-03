# 온보딩 friction 제거 (SPEC-ONBOARD-LITE-001) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 신규 유저가 카드 퍼널에 가로막히지 않고 첫 actionable 메시지에서 바로 추천을 받도록 온보딩 카드 서브그래프를 완전 제거하고 경량 first-touch로 대체한다.

**Architecture:** 신규 유저(`sess.onboarded_at IS NULL`) + actionable(photo/url/text) → `ingest` 노드가 인라인 그리팅 1줄 + `onboarded_at` 마킹 후 평소 라우팅으로 같은 턴 진행. `/start`-only 신규 유저 → 기존 `intro` 노드(짧은 소개 + 종료). 온보딩 9 노드 + onboarding_cards/values + pinterest_url + apify + routing 술어 + `seed_from_onboarding` + 테스트 ~17 완전 삭제. 취향→검색 루프 닫기는 범위 밖.

**Tech Stack:** Python 3.13, FastAPI, LangGraph StateGraph, Pydantic v2, pytest, ruff, uv. 베이스: `origin/dev` @ ecad552, 브랜치 `fix/onboarding-redesign`.

**Spec:** `docs/superpowers/specs/2026-05-19-onboarding-friction-removal-design.md`

---

## File Structure

| 파일 | 책임 | 작업 |
|------|------|------|
| `app/channels/reset_keywords.py` | `/reset` 키워드 단일 소스 (onboarding_values에서 분리) | Create |
| `app/graphs/nodes/ingest.py` | first-touch 그리팅 + `/reset` taste clear + returning-/start ack 인라인 | Modify |
| `app/graphs/fashion_bot.py` | `_route_after_ingest_v2` 재작성, 온보딩 노드/엣지/import 제거 | Modify |
| `app/graphs/routing.py` | 온보딩 술어 7개 제거 | Modify |
| `app/graphs/nodes/intro.py` | 진입 조건 docstring/플래그 참조 정리 | Modify |
| `app/infrastructure/memory/session.py` | onboarding 필드 6개 제거 (`onboarded_at` 존치) | Modify |
| `app/infrastructure/memory/session_pg.py` | SELECT/UPSERT에서 onboarding 컬럼 제거 (DB 컬럼 물리 존치) | Modify |
| `app/infrastructure/memory/taste_profile.py` / `taste_profile_pg.py` | `seed_from_onboarding` 제거 | Modify |
| `app/core/config.py` | ONBOARDING_*/PINTEREST_*/APIFY_* 키 11 + 검증자 6 제거 | Modify |
| `.env.example`, `docs/infra/env.md`, `CLAUDE.md` | 문서 동기화 | Modify |
| 온보딩 노드/채널/프로바이더 9+3+1 파일 | 완전 삭제 | Delete |
| `tests/test_onboarding/` 외 ~17 | 완전 삭제 | Delete |
| `.moai/specs/SPEC-ONBOARD-LITE-001/spec.md` | 신규 SPEC | Create |

---

## Task 1: `/reset` 키워드 단일 소스 분리

**근거:** `is_restart_keyword`는 삭제 대상 `onboarding_values.py`에 있다. `/reset`는 taste clear로 용도 변경되어 존속하므로 작은 독립 모듈로 분리한다.

**Files:**
- Create: `app/channels/reset_keywords.py`
- Test: `tests/test_channels/test_reset_keywords.py`

- [ ] **Step 1: 실패 테스트 작성**

Create `tests/test_channels/test_reset_keywords.py`:

```python
from app.channels.reset_keywords import RESET_KEYWORDS, is_reset_keyword


def test_exact_match_case_insensitive():
    assert is_reset_keyword("/reset")
    assert is_reset_keyword("  /RESET  ")
    assert is_reset_keyword("취향 초기화")
    assert is_reset_keyword("reset taste")


def test_non_match():
    assert not is_reset_keyword(None)
    assert not is_reset_keyword("")
    assert not is_reset_keyword("reset")
    assert not is_reset_keyword("/start")
    assert not is_reset_keyword("미니멀한 코트 찾아줘")


def test_keyword_set_frozen():
    assert isinstance(RESET_KEYWORDS, frozenset)
    assert "/reset" in RESET_KEYWORDS
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_channels/test_reset_keywords.py -q`
Expected: FAIL — `ModuleNotFoundError: app.channels.reset_keywords`

- [ ] **Step 3: 최소 구현**

Create `app/channels/reset_keywords.py`:

```python
"""SPEC-ONBOARD-LITE-001 — `/reset` keyword single source.

Repurposed from the retired SPEC-ONBOARD-CARDS-001 restart-keyword set.
`/reset` no longer re-enters an onboarding card flow (removed); it now
clears the caller's TasteProfile. Kept as a tiny standalone module so the
deletion of `onboarding_values.py` does not strand the predicate.
"""

from __future__ import annotations

# Hangul `\b` word boundary is unreliable — exact (stripped, lowercased)
# match only, mirroring the retired is_restart_keyword contract.
RESET_KEYWORDS: frozenset[str] = frozenset({"/reset", "취향 초기화", "reset taste"})


def is_reset_keyword(text: str | None) -> bool:
    """True iff `text` (stripped, casefolded) is an exact taste-reset trigger."""
    if not text:
        return False
    return text.strip().lower() in RESET_KEYWORDS
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_channels/test_reset_keywords.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: 커밋**

```bash
git add app/channels/reset_keywords.py tests/test_channels/test_reset_keywords.py
git commit -m "feat(SPEC-ONBOARD-LITE-001): /reset 키워드 단일 소스 분리

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 2: ingest 인라인 first-touch + `/reset` + returning-/start ack

**근거:** `ingest`는 이미 implicit-feedback / clarify / hybrid-card 콜백을 인라인 처리하는 노드(side-effect 허용). first-touch 그리팅·taste reset·returning `/start` ack를 동일 패턴으로 추가한다. 라우팅 결정은 Task 3.

**Files:**
- Create: `app/graphs/nodes/_first_touch.py` (헬퍼 — ingest 비대화 방지)
- Modify: `app/graphs/nodes/ingest.py` (헬퍼 호출 1줄 추가)
- Test: `tests/test_graph_nodes/test_first_touch.py`

- [ ] **Step 1: 실패 테스트 작성**

Create `tests/test_graph_nodes/test_first_touch.py`:

```python
import asyncio
from datetime import datetime

import pytest

from app.graphs.nodes._first_touch import maybe_first_touch


class _Adapter:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append(text)


class _Msg:
    def __init__(self, text=None, photo_file_id=None, urls=None, callback_data=None):
        self.text = text
        self.photo_file_id = photo_file_id
        self.urls = urls or []
        self.callback_data = callback_data


class _State:
    def __init__(self, msg, chat_id=1, from_user_id=7):
        self.message = msg
        self.chat_id = chat_id
        self.from_user_id = from_user_id
        self.thread_id = None
        self.turn_no = 1


class _Sess:
    def __init__(self, onboarded_at=None):
        self.onboarded_at = onboarded_at
        self.lang = "ko"


@pytest.mark.asyncio
async def test_new_user_actionable_photo_greets_and_marks(monkeypatch):
    adapter = _Adapter()
    sess = _Sess(onboarded_at=None)
    state = _State(_Msg(photo_file_id="abc"))
    deleted = []
    await maybe_first_touch(
        state, sess, adapter,
        taste_delete=lambda uk: deleted.append(uk),
        breadcrumbs=[],
    )
    assert any("kiko" in s for s in adapter.sent)
    assert isinstance(sess.onboarded_at, datetime)
    assert deleted == []


@pytest.mark.asyncio
async def test_new_user_start_only_no_greeting_no_mark():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=None)
    state = _State(_Msg(text="/start"))
    await maybe_first_touch(state, sess, adapter, taste_delete=lambda uk: None, breadcrumbs=[])
    assert adapter.sent == []          # intro 노드가 처리 — ingest는 침묵
    assert sess.onboarded_at is None    # intro 노드가 마킹


@pytest.mark.asyncio
async def test_reset_keyword_clears_taste_and_acks():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=datetime.now())
    state = _State(_Msg(text="/reset"))
    deleted = []
    await maybe_first_touch(state, sess, adapter, taste_delete=lambda uk: deleted.append(uk), breadcrumbs=[])
    assert deleted == ["u:7"]
    assert any("초기화" in s or "reset" in s.lower() for s in adapter.sent)


@pytest.mark.asyncio
async def test_returning_user_actionable_no_greeting():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=datetime.now())
    state = _State(_Msg(text="미니멀 코트"))
    await maybe_first_touch(state, sess, adapter, taste_delete=lambda uk: None, breadcrumbs=[])
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_returning_user_start_only_acks():
    adapter = _Adapter()
    sess = _Sess(onboarded_at=datetime.now())
    state = _State(_Msg(text="/start"))
    await maybe_first_touch(state, sess, adapter, taste_delete=lambda uk: None, breadcrumbs=[])
    assert len(adapter.sent) == 1       # 가벼운 ready ack
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_graph_nodes/test_first_touch.py -q`
Expected: FAIL — `ModuleNotFoundError: app.graphs.nodes._first_touch`

- [ ] **Step 3: 헬퍼 구현**

Create `app/graphs/nodes/_first_touch.py`:

```python
"""SPEC-ONBOARD-LITE-001 — ingest-inline first-touch / reset / start-ack.

Side-effecting helper invoked from `ingest` (the node already runs inline
side effects for implicit-feedback / clarify / hybrid-card callbacks). Pure
routing stays in `_route_after_ingest_v2`.

Behavior matrix (gated on `sess.onboarded_at` + message shape):
  - `/reset` keyword (any user)        → taste_delete(user_key) + ack
  - new user + actionable              → 1-line greeting + mark onboarded_at
  - new user + `/start`-only           → no-op (intro node handles + marks)
  - returning user + `/start`-only     → light ready ack
  - returning user + actionable        → no-op
`actionable` = photo OR urls OR (text and text.strip() != "/start").
contentless (no text/cb/url/photo) is never first-touch — handled by the
router's silent-END guard.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.channels.reset_keywords import is_reset_keyword
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import get_taste_store, user_key_for
from app.observability.conversation_log import emit

logger = logging.getLogger(__name__)

_GREET_KO = "안녕! 난 kiko야 🐱 바로 찾아볼게요."
_GREET_EN = "Hey! I'm kiko 🐱 — finding it now."
_RESET_KO = "취향 기록을 초기화했어요 🐱 새로 시작해요!"
_RESET_EN = "Cleared your taste history 🐱 fresh start!"
_READY_KO = "어 왔구나 🐱 뭐 찾아줄까요?"
_READY_EN = "Hey, welcome back 🐱 what are we looking for?"


def _is_start_only(text: str | None) -> bool:
    return (text or "").strip().lower() == "/start"


def _is_actionable(msg: Any) -> bool:
    if msg.photo_file_id or msg.urls:
        return True
    t = (msg.text or "").strip()
    return bool(t) and t.lower() != "/start"


async def maybe_first_touch(
    state: Any,
    sess: Any,
    adapter: Any,
    *,
    taste_delete: Callable[[str], None] | None = None,
    breadcrumbs: list[str],
) -> None:
    """Run the first-touch / reset / ready-ack side effects. Never raises."""
    msg = state.message
    user_key = user_key_for(state.from_user_id, state.chat_id)
    lang = getattr(sess, "lang", "en")

    # 1. /reset → clear taste profile + ack (any user).
    if is_reset_keyword(msg.text):
        try:
            (taste_delete or get_taste_store().delete)(user_key)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [first_touch] taste delete failed")
        try:
            await adapter.send_text(state.chat_id, _RESET_KO if lang == "ko" else _RESET_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] reset ack send best-effort", exc_info=True)
        breadcrumbs.append("first_touch: reset taste cleared")
        return

    is_new = getattr(sess, "onboarded_at", None) is None

    # 2. new user + actionable → greeting + mark onboarded_at (same turn proceeds).
    if is_new and _is_actionable(msg):
        try:
            await adapter.send_text(state.chat_id, _GREET_KO if lang == "ko" else _GREET_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] greeting send best-effort", exc_info=True)
        try:
            sess.onboarded_at = datetime.now(UTC)
            get_store().update(sess)
        except Exception:  # noqa: BLE001
            logger.exception("🐱 [first_touch] onboarded_at persist failed")
        try:
            emit(
                event_type="bot_text",
                user_key=user_key,
                chat_id=state.chat_id,
                thread_id=getattr(state, "thread_id", None),
                turn_no=getattr(state, "turn_no", 1),
                payload={"flow": "first_touch", "chunk_index": 0, "total_chunks": 1},
            )
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] bot_text emit best-effort", exc_info=True)
        breadcrumbs.append("first_touch: greeted + marked")
        return

    # 3. returning user + /start-only → light ready ack.
    if not is_new and _is_start_only(msg.text) and not (msg.photo_file_id or msg.urls):
        try:
            await adapter.send_text(state.chat_id, _READY_KO if lang == "ko" else _READY_EN)
        except Exception:  # noqa: BLE001
            logger.debug("[first_touch] ready ack send best-effort", exc_info=True)
        breadcrumbs.append("first_touch: returning /start ack")
        return

    # new user + /start-only → no-op (intro node); returning + actionable → no-op.
    return
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_graph_nodes/test_first_touch.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: ingest에 헬퍼 배선**

In `app/graphs/nodes/ingest.py`, add import after line 18 (`from app.infrastructure.memory.taste_profile import user_key_for`):

```python
from app.graphs.nodes._first_touch import maybe_first_touch
from app.graphs.nodes._adapter_ctx import get_adapter
```

Then in `ingest()`, insert immediately BEFORE the `_emit_intent_routed(state)` call (currently line 242):

```python
    # SPEC-ONBOARD-LITE-001 — first-touch greeting / /reset taste clear /
    # returning-/start ack. Inline side effects (same pattern as the
    # clarify/hybrid-card handlers above). Routing decision is in
    # _route_after_ingest_v2.
    try:
        await maybe_first_touch(state, sess, get_adapter(), breadcrumbs=breadcrumbs)
    except Exception as exc:  # noqa: BLE001 — never block webhook
        logger.debug("[ingest] first_touch handling failed: %r", exc)
```

- [ ] **Step 6: ingest 노드 회귀 테스트**

Run: `uv run pytest tests/ -q -k "ingest or first_touch"`
Expected: PASS (no regressions)

- [ ] **Step 7: 커밋**

```bash
git add app/graphs/nodes/_first_touch.py app/graphs/nodes/ingest.py tests/test_graph_nodes/test_first_touch.py
git commit -m "feat(SPEC-ONBOARD-LITE-001): ingest 인라인 first-touch/reset/ready-ack

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 3: `_route_after_ingest_v2` 재작성 + fashion_bot 온보딩 노드/엣지 제거

**근거:** 라우터에서 온보딩 게이트 3분기를 제거하고, 신규 유저 `/start`-only → `intro`, `/reset`·contentless → `__end__` 분기를 추가한다. 온보딩 노드 등록/엣지/import도 함께 제거.

**Files:**
- Modify: `app/graphs/fashion_bot.py`
- Test: `tests/test_graph_nodes/test_route_after_ingest.py`

- [ ] **Step 1: 실패 테스트 작성**

Create `tests/test_graph_nodes/test_route_after_ingest.py`:

```python
import pytest

from app.graphs.fashion_bot import build_graph  # noqa: F401  (import smoke)
from app.graphs import fashion_bot
from app.infrastructure.memory.session import Session, SessionState, get_store, set_store, InMemorySessionStore


class _Msg:
    def __init__(self, text=None, photo_file_id=None, urls=None, callback_data=None, callback_query_id=None):
        self.text = text
        self.photo_file_id = photo_file_id
        self.urls = urls or []
        self.callback_data = callback_data
        self.callback_query_id = callback_query_id


class _State:
    def __init__(self, msg, chat_id):
        self.message = msg
        self.chat_id = chat_id
        self.from_user_id = 7
        self.selected_item_index = None


@pytest.fixture(autouse=True)
def _fresh_store():
    set_store(InMemorySessionStore())
    yield
    set_store(InMemorySessionStore())


def _router():
    # _route_after_ingest_v2 is a closure built inside build_graph; expose it
    # via the module-level test seam added in Step 3.
    return fashion_bot._route_after_ingest_v2


def test_new_user_start_only_routes_to_intro():
    s = get_store().get_or_create(100)
    assert s.onboarded_at is None
    assert _router()(_State(_Msg(text="/start"), 100)) == "intro"


def test_new_user_photo_routes_to_resolve_image():
    get_store().get_or_create(101)
    assert _router()(_State(_Msg(photo_file_id="x"), 101)) == "resolve_image"


def test_new_user_text_routes_to_agent():
    get_store().get_or_create(102)
    assert _router()(_State(_Msg(text="미니멀 코트"), 102)) == "agent"


def test_reset_keyword_routes_to_end():
    s = get_store().get_or_create(103)
    s.onboarded_at = __import__("datetime").datetime.now()
    assert _router()(_State(_Msg(text="/reset"), 103)) == "__end__"


def test_contentless_routes_to_end():
    get_store().get_or_create(104)
    assert _router()(_State(_Msg(), 104)) == "__end__"


def test_returning_user_text_routes_to_agent():
    s = get_store().get_or_create(105)
    s.onboarded_at = __import__("datetime").datetime.now()
    assert _router()(_State(_Msg(text="안녕"), 105)) == "agent"


def test_graph_builds_without_onboarding_nodes():
    g = build_graph()
    assert g is not None  # no ImportError from removed onboard_* modules
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run pytest tests/test_graph_nodes/test_route_after_ingest.py -q`
Expected: FAIL — `AttributeError: module 'app.graphs.fashion_bot' has no attribute '_route_after_ingest_v2'`

- [ ] **Step 3: fashion_bot.py 재작성**

In `app/graphs/fashion_bot.py`:

(a) Remove these imports (lines 24-32 region):
```python
from app.graphs.nodes.onboard_color import onboard_color
from app.graphs.nodes.onboard_fit import onboard_fit
from app.graphs.nodes.onboard_intro import onboard_intro
from app.graphs.nodes.onboard_mood import onboard_mood
from app.graphs.nodes.onboard_pinterest import onboard_pinterest
from app.graphs.nodes.pinterest_ingest import pinterest_ingest
```
Change `from app.graphs.routing import _route_after_onboard_fit, _route_after_resolve` → `from app.graphs.routing import _route_after_resolve`.

(b) Delete the `_ONBOARD_FIT_BRANCHES` dict (lines 53-57).

(c) Remove onboard node registrations (lines 79-85: the 6 `builder.add_node("onboard_*", ...)` + `pinterest_ingest`).

(d) Replace the `_route_after_ingest_v2` closure body. Delete the onboarding/continuous-pinterest/first_touch_intro_required block (lines 88-110) and replace with:

```python
    def _route_after_ingest_v2(state: WorkingState) -> str:
        from app.channels.reset_keywords import is_reset_keyword
        from app.infrastructure.memory.session import SessionState, get_store

        msg = state.message
        cb = msg.callback_data or ""
        text = (msg.text or "").strip()

        # /reset — ingest already cleared taste + acked; terminate silently.
        if is_reset_keyword(msg.text):
            return "__end__"

        # SPEC-AGENT-V2-REACT §15 Decision 2 — contentless Update silent END.
        # Checked before first-touch so a spurious blank Update never triggers
        # the intro.
        if not text and not msg.callback_data and not msg.urls and not msg.photo_file_id:
            return "__end__"

        sess = get_store().get_or_create(state.chat_id)
        is_new = getattr(sess, "onboarded_at", None) is None

        # New user + /start-only (no actionable content) → service intro.
        if is_new and text.lower() == "/start" and not msg.photo_file_id and not msg.urls and not cb:
            return "intro"

        # Picker callback → pick_item (deterministic).
        if cb.startswith("item:"):
            return "pick_item"
        # Hybrid result-card callbacks fully serviced by ingest — terminal.
        if cb.startswith("card:like:") or cb == "cards:more":
            return "__end__"
        # Photo / URL → vision pre-step.
        if msg.photo_file_id or msg.urls:
            return "resolve_image"
        # AWAITING_ITEM_PICK digit-pick fallback.
        if msg.text and sess.state == SessionState.AWAITING_ITEM_PICK:
            return "pick_item"
        # Everything else (text incl. greetings, clarify:/crit:* callbacks) →
        # agent. ingest inline-handled greeting/ack already.
        return "agent"
```

(e) After `GRAPH = build_graph()` (end of file), add the test seam so the closure is importable:

```python
# Test seam — expose the routing closure for unit tests (the closure itself
# is rebuilt per build_graph(); this binds the last-built instance).
def _route_after_ingest_v2(state: WorkingState) -> str:  # pragma: no cover - rebound below
    raise RuntimeError("placeholder — rebound by build_graph()")
```

Then inside `build_graph()`, immediately after the `def _route_after_ingest_v2` definition, add:
```python
        globals()["_route_after_ingest_v2"] = _route_after_ingest_v2
```

(f) Remove onboard edges (lines 187-193): the 4 `builder.add_edge("onboard_*", END)` + `builder.add_conditional_edges("onboard_fit", ...)` + `builder.add_edge("pinterest_ingest", END)`. Also remove `"pinterest_ingest"` / `"onboard_*"` keys from `ingest_branches_v2` (lines 151-156), keeping `"intro": "intro"`.

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run pytest tests/test_graph_nodes/test_route_after_ingest.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: 커밋**

```bash
git add app/graphs/fashion_bot.py tests/test_graph_nodes/test_route_after_ingest.py
git commit -m "feat(SPEC-ONBOARD-LITE-001): _route_after_ingest_v2 재작성 + 온보딩 노드/엣지 제거

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 4: routing.py 온보딩 술어 제거

**Files:**
- Modify: `app/graphs/routing.py`
- Test: 기존 `tests/` 전체 회귀

- [ ] **Step 1: 제거 대상 확인**

Run: `grep -n "onboarding_required\|_resolve_onboard_stage_target\|is_continuous_pinterest\|first_touch_intro_required\|_route_after_onboard_fit\|_is_restart_keyword\|_ONBOARDING_ACTIVE_STAGES" app/graphs/routing.py`
Expected: 라인 39-231 영역의 정의들.

- [ ] **Step 2: 심볼 삭제**

In `app/graphs/routing.py` delete: `_ONBOARDING_ACTIVE_STAGES` (line 39), `_is_restart_keyword` (42-46), `onboarding_required` (49-83), `first_touch_intro_required` (86-110), `_resolve_onboard_stage_target` (113-158), `is_continuous_pinterest` (161-216), `_route_after_onboard_fit` (219-231). Keep `_route_after_resolve`, `_is_vision_fallback`, `_is_weak_vision*`. Remove now-unused imports (`datetime`, `UTC`, `settings`, `Session`, `get_store`) if no remaining reference — verify with grep before removing each.

- [ ] **Step 3: import 잔존 0 확인**

Run: `grep -rn "onboarding_required\|first_touch_intro_required\|is_continuous_pinterest\|_resolve_onboard_stage_target\|_route_after_onboard_fit" app/ --include="*.py"`
Expected: 출력 없음 (all callers were in fashion_bot.py, removed in Task 3).

- [ ] **Step 4: 모듈 import smoke + ruff**

Run: `uv run python -c "import app.graphs.routing, app.graphs.fashion_bot" && uv run ruff check app/graphs/routing.py app/graphs/fashion_bot.py`
Expected: no ImportError, ruff clean.

- [ ] **Step 5: 커밋**

```bash
git add app/graphs/routing.py
git commit -m "refactor(SPEC-ONBOARD-LITE-001): routing.py 온보딩 술어 7개 제거

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 5: 온보딩 노드/채널/프로바이더 파일 완전 삭제

**근거:** Task 3·4에서 모든 참조를 제거했으므로 파일을 삭제한다. `link_resolver.py`는 핵심 흐름이 쓰므로 **삭제 금지**.

**Files (Delete):**
- `app/graphs/nodes/onboard_intro.py` `onboard_mood.py` `onboard_color.py` `onboard_fit.py` `onboard_pinterest.py` `pinterest_ingest.py` `_onboard_helpers.py` `_onboard_stage.py` `_pinterest_helpers.py`
- `app/channels/onboarding_cards.py` `onboarding_values.py` `pinterest_url.py`
- `app/providers/apify.py`

- [ ] **Step 1: 잔존 참조 사전 스윕**

Run:
```bash
grep -rn "onboard_intro\|onboard_mood\|onboard_color\|onboard_fit\|onboard_pinterest\|pinterest_ingest\|_onboard_helpers\|_onboard_stage\|_pinterest_helpers\|onboarding_cards\|onboarding_values\|pinterest_url\|providers.apify\|providers import.*apify\|run_pinterest_scrape" app/ --include="*.py" | grep -v "^app/graphs/nodes/onboard\|^app/graphs/nodes/pinterest_ingest\|^app/graphs/nodes/_onboard\|^app/graphs/nodes/_pinterest\|^app/channels/onboarding\|^app/channels/pinterest_url\|^app/providers/apify"
```
Expected: 출력 없음. 출력이 있으면 해당 호출자를 먼저 정리(대개 import 한 줄) 후 진행.

- [ ] **Step 2: 파일 삭제**

```bash
git rm app/graphs/nodes/onboard_intro.py app/graphs/nodes/onboard_mood.py \
  app/graphs/nodes/onboard_color.py app/graphs/nodes/onboard_fit.py \
  app/graphs/nodes/onboard_pinterest.py app/graphs/nodes/pinterest_ingest.py \
  app/graphs/nodes/_onboard_helpers.py app/graphs/nodes/_onboard_stage.py \
  app/graphs/nodes/_pinterest_helpers.py \
  app/channels/onboarding_cards.py app/channels/onboarding_values.py \
  app/channels/pinterest_url.py app/providers/apify.py
```

- [ ] **Step 3: 전체 import smoke**

Run: `uv run python -c "import app.main"`
Expected: no ImportError. 실패 시 에러의 import를 추적해 잔존 참조 제거.

- [ ] **Step 4: 커밋**

```bash
git commit -m "refactor(SPEC-ONBOARD-LITE-001): 온보딩 노드/채널/프로바이더 13파일 완전 삭제

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 6: `seed_from_onboarding` 제거 (taste_profile / taste_profile_pg)

**Files:**
- Modify: `app/infrastructure/memory/taste_profile.py`, `app/infrastructure/memory/taste_profile_pg.py`
- Test: 기존 `tests/test_memory*` 회귀

- [ ] **Step 1: 호출자 0건 확인**

Run: `grep -rn "seed_from_onboarding" app/ --include="*.py"`
Expected: 정의부(Protocol line 176, InMemory 237, taste_profile_pg 62/76/150) 외 호출자 없음 (호출자는 Task 5에서 삭제된 `_onboard_helpers`/`_pinterest_helpers`에만 존재했음).

- [ ] **Step 2: 심볼 삭제**

`app/infrastructure/memory/taste_profile.py`: Protocol 메서드 시그니처(line 176) + `InMemoryTasteProfileStore.seed_from_onboarding` (236-273) 삭제.
`app/infrastructure/memory/taste_profile_pg.py`: `seed_from_onboarding` (62-76) + `_aseed_from_onboarding` (149-끝부분) 삭제. `ONBOARDING_SEED_MAX_WEIGHT` 참조도 함께 사라짐.

- [ ] **Step 3: taste store 회귀**

Run: `uv run pytest tests/ -q -k "taste" --ignore=tests/test_onboarding`
Expected: PASS (seed_from_onboarding 외 TasteProfile 동작 무회귀). 삭제 대상 `test_taste_seed*`는 Task 8에서 제거 — 여기서 실패하면 `--deselect` 로 격리하고 Task 8에서 일괄 삭제.

- [ ] **Step 4: 커밋**

```bash
git add app/infrastructure/memory/taste_profile.py app/infrastructure/memory/taste_profile_pg.py
git commit -m "refactor(SPEC-ONBOARD-LITE-001): seed_from_onboarding 제거 (호출자 0)

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 7: Session 모델 onboarding 필드 제거 (PG 컬럼 물리 존치)

**Files:**
- Modify: `app/infrastructure/memory/session.py`, `app/infrastructure/memory/session_pg.py`
- Test: `tests/test_memory_pg/` 회귀 (삭제 대상 제외)

- [ ] **Step 1: session.py 필드 제거**

In `app/infrastructure/memory/session.py` `@dataclass Session`, delete lines 88-97 EXCEPT keep `onboarded_at`:
```python
    # SPEC-ONBOARD-LITE-001 — onboarded_at retained as the sole first-touch
    # discriminator. The card-flow staging fields were removed (subgraph
    # retired); PG columns are kept physically (no destructive migration).
    onboarded_at: datetime | None = None
```
Remove: `onboard_stage`, `onboard_selections`, `onboard_card_message_id`, `last_pinterest_scrape_url`, `last_pinterest_scrape_at`, `last_pinterest_pins`.

- [ ] **Step 2: session_pg.py 컬럼 참조 제거**

Run first: `grep -n "onboard_stage\|onboard_selections\|onboard_card_message_id\|last_pinterest_scrape_url\|last_pinterest_scrape_at\|last_pinterest_pins" app/infrastructure/memory/session_pg.py`

In `app/infrastructure/memory/session_pg.py`, remove these 6 identifiers from: the SELECT column list, the INSERT column list + `VALUES` placeholders, the `ON CONFLICT ... SET` list, and the params tuple / row-hydration mapping. **Do NOT issue an ALTER TABLE** — DB columns stay (nullable, ignored). Keep `onboarded_at` everywhere it appears.

- [ ] **Step 3: 회귀 확인**

Run: `uv run python -c "import app.main" && uv run pytest tests/test_memory_pg -q --ignore=tests/test_memory_pg/test_session_store_onboarding_columns.py --ignore=tests/test_memory_pg/test_taste_seed_onboarding.py`
Expected: PASS (잔존 세션 컬럼 round-trip 무회귀).

- [ ] **Step 4: 커밋**

```bash
git add app/infrastructure/memory/session.py app/infrastructure/memory/session_pg.py
git commit -m "refactor(SPEC-ONBOARD-LITE-001): Session 온보딩 필드 6 제거 (PG 컬럼 존치)

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 8: config.py + .env.example + docs 정리

**Files:**
- Modify: `app/core/config.py`, `.env.example`, `docs/infra/env.md`
- Test: `tests/test_onboarding/test_config_validators.py`는 Task 9에서 삭제 — 여기서는 config import만 검증

- [ ] **Step 1: config.py 키/검증자 제거**

In `app/core/config.py` delete lines 175-195 (APIFY_TOKEN, APIFY_PINTEREST_ACTOR, APIFY_PINTEREST_MAX_ITEMS, APIFY_PINTEREST_CONCURRENCY, PINTEREST_BOOTSTRAP_ENABLED, PINTEREST_INGEST_CACHE_TTL_S, PINTEREST_MAX_PINS_PER_TURN, PINTEREST_CONTINUOUS_RATELIMIT_S, PINTEREST_CONTINUOUS_ENABLED, ONBOARDING_CARDS_ENABLED, ONBOARDING_SEED_MAX_WEIGHT) and the 6 `@field_validator` blocks at lines 235-274 (`_validate_apify_max_items`, `_validate_apify_concurrency`, `ONBOARDING_SEED_MAX_WEIGHT` validator, `PINTEREST_MAX_PINS_PER_TURN`, `PINTEREST_INGEST_CACHE_TTL_S`, `PINTEREST_CONTINUOUS_RATELIMIT_S`). Keep all non-onboarding settings/validators.

- [ ] **Step 2: .env.example + docs/infra/env.md 정리**

Run: `grep -n "ONBOARDING\|PINTEREST\|APIFY" .env.example docs/infra/env.md`
Remove every matched line in `.env.example`; in `docs/infra/env.md` delete the onboarding/pinterest/apify flag rows + the `ONBOARDING_CARDS_ENABLED`/`PINTEREST_BOOTSTRAP_ENABLED` bullet.

- [ ] **Step 3: config import + settings 인스턴스화 검증**

Run: `uv run python -c "from app.core.config import settings; assert not hasattr(settings, 'ONBOARDING_CARDS_ENABLED'); print('ok')"`
Expected: `ok`

- [ ] **Step 4: 커밋**

```bash
git add app/core/config.py .env.example docs/infra/env.md
git commit -m "refactor(SPEC-ONBOARD-LITE-001): 온보딩/pinterest/apify env 키 11+검증자 6 제거

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 9: 온보딩 테스트 삭제 + 공유 테스트 정리 + 전체 그린

**Files (Delete):**
- `tests/test_onboarding/` 디렉터리 전체
- `tests/test_memory_pg/test_session_store_onboarding_columns.py`
- `tests/test_memory_pg/test_taste_seed_onboarding.py`
- `tests/test_graph_nodes/test_routing_onboarding.py`

- [ ] **Step 1: 디렉터리/파일 삭제**

```bash
git rm -r tests/test_onboarding \
  tests/test_memory_pg/test_session_store_onboarding_columns.py \
  tests/test_memory_pg/test_taste_seed_onboarding.py \
  tests/test_graph_nodes/test_routing_onboarding.py
```

- [ ] **Step 2: 공유 테스트 내 온보딩 의존 식별**

Run: `grep -rln "onboard\|seed_from_onboarding\|onboarding_cards\|onboarding_values\|pinterest_url\|ONBOARDING_CARDS_ENABLED\|first_touch_intro_required\|onboarding_required" tests/ --include="*.py"`
For each remaining file: open it, remove only the onboarding-specific test functions / fixtures / imports (do NOT delete non-onboarding tests in the same file). If a whole file is onboarding-only, `git rm` it.

- [ ] **Step 3: 전체 테스트 그린**

Run: `uv run pytest -q`
Expected: PASS, 0 failed, 0 error. Collection 에러(삭제 모듈 import) 발견 시 해당 테스트의 잔존 import 제거.

- [ ] **Step 4: ruff 전체**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean (unused import 0).

- [ ] **Step 5: 커밋**

```bash
git add -A tests/
git commit -m "test(SPEC-ONBOARD-LITE-001): 온보딩 테스트 ~17 삭제 + 공유 테스트 정리

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

> NOTE: `git add -A tests/` 는 tests/ 하위로 스코프가 한정되므로 전역 `git add -A` 금지 규칙 위반 아님. 그래도 `git status` 로 의도된 파일만 staged 인지 확인 후 커밋.

---

## Task 10: intro 노드 정리 + 문서 동기화 + SPEC

**Files:**
- Modify: `app/graphs/nodes/intro.py` (docstring/플래그 언급만 정정 — 동작 유지)
- Modify: `CLAUDE.md`
- Create: `.moai/specs/SPEC-ONBOARD-LITE-001/spec.md`
- Modify: `.moai/specs/SPEC-ONBOARD-CARDS-001/spec.md` (retirement note)

- [ ] **Step 1: intro.py docstring 정정**

In `app/graphs/nodes/intro.py` docstring, replace the `ONBOARDING_CARDS_ENABLED=false` 전제 문구를 SPEC-ONBOARD-LITE-001 기준으로 갱신: "Reached only for a brand-new user whose first message is `/start`-only (router gate). Sends one service intro, marks `onboarded_at`, ends the turn." 동작 코드는 변경 없음 (이미 onboarded_at 마킹 + END).

Run: `grep -n "ONBOARDING_CARDS_ENABLED\|SPEC-AGENT-V2-REACT" app/graphs/nodes/intro.py` — 플래그 언급 주석만 갱신, 로직 라인 미변경.

- [ ] **Step 2: CLAUDE.md 동기화**

In `CLAUDE.md`: (a) 책임분리 표에서 `Apify` 행 + `온보딩 카드(SPEC-ONBOARD-CARDS-001)` 문구 제거, (b) 디렉토리 트리 `nodes/` 설명에서 onboard_* / pinterest_ingest 제거 → "ingest, resolve_image, vision_node, pick_item, ask_clarify, apply_clarify, agent, intro + _trace.py", (c) `providers/` 설명에서 `ApifyProvider` 제거, (d) 핵심 파일 표에서 onboard_*/pinterest/_onboard_helpers/_onboard_stage/_pinterest_helpers/onboarding_cards/onboarding_values/pinterest_url/apify 행 삭제, `routing.py` 설명에서 온보딩 분기 문구 제거, `intro.py` 설명을 SPEC-ONBOARD-LITE-001로 갱신, (e) 환경변수 섹션에서 `ONBOARDING_CARDS_ENABLED ... ONBOARDING_SEED_MAX_WEIGHT` bullet 삭제.

- [ ] **Step 3: SPEC 파일**

Create `.moai/specs/SPEC-ONBOARD-LITE-001/spec.md` (EARS 요건: REQ-OBL-001 신규+actionable→그리팅+같은턴추천, REQ-OBL-002 신규+/start-only→intro, REQ-OBL-003 /reset→TasteProfile clear, REQ-OBL-004 카드 서브그래프 미존재, REQ-OBL-005 PG 컬럼 물리 존치). 본문은 설계 문서 `docs/superpowers/specs/2026-05-19-onboarding-friction-removal-design.md` 를 참조 링크.

In `.moai/specs/SPEC-ONBOARD-CARDS-001/spec.md` prepend:
```markdown
> ⚠️ RETIRED 2026-05-19 — superseded by SPEC-ONBOARD-LITE-001 (onboarding
> card subgraph fully removed; friction-removal redesign). Kept for history.
```

- [ ] **Step 4: 검증**

Run: `uv run python -c "import app.graphs.nodes.intro" && test -f .moai/specs/SPEC-ONBOARD-LITE-001/spec.md && echo ok`
Expected: `ok`

- [ ] **Step 5: 커밋**

```bash
git add app/graphs/nodes/intro.py CLAUDE.md .moai/specs/SPEC-ONBOARD-LITE-001/spec.md .moai/specs/SPEC-ONBOARD-CARDS-001/spec.md
git commit -m "docs(SPEC-ONBOARD-LITE-001): intro/CLAUDE.md 동기화 + SPEC retire/신설

Co-Authored-By: Claude <noreply@anthropic.com>

🗿 MoAI <email@mo.ai.kr>"
```

---

## Task 11: 최종 회귀 게이트

- [ ] **Step 1: import 잔존 스윕**

Run:
```bash
grep -rn "onboard_\|onboarding_\|pinterest_url\|providers.apify\|seed_from_onboarding\|ONBOARDING_CARDS_ENABLED\|first_touch_intro_required\|onboarding_required\|is_continuous_pinterest" app/ --include="*.py"
```
Expected: 출력 없음 (잔존 0). `onboarded_at` 매칭은 정상(존치) — `onboard_` prefix grep 에 안 걸림.

- [ ] **Step 2: 전체 테스트 + 린트 + 포맷**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all PASS, 0 fail/error, ruff clean.

- [ ] **Step 3: 앱 부팅 smoke**

Run: `uv run python -c "import app.main; from app.graphs.fashion_bot import GRAPH; print('graph ok', GRAPH is not None)"`
Expected: `graph ok True`

- [ ] **Step 4: 핵심 핀링크 회귀 (link_resolver 존치 검증)**

Run: `uv run pytest tests/ -q -k "resolve_image or link_resolver"`
Expected: PASS (Pinterest/og:image → 추천 핵심 흐름 무회귀).

- [ ] **Step 5: 최종 커밋 (필요 시)**

```bash
git status --porcelain   # clean expected; 잔여 변경 있으면 명시 add 후 commit
```

---

## Self-Review

**1. Spec coverage:**
- REQ-OBL-001 (신규+actionable→그리팅+같은턴) → Task 2 + Task 3 ✓
- REQ-OBL-002 (신규+/start-only→intro) → Task 3 (router) + Task 10 (intro docstring) ✓
- REQ-OBL-003 (/reset→taste clear) → Task 1 + Task 2 ✓
- REQ-OBL-004 (카드 서브그래프 미존재) → Task 3/4/5 ✓
- REQ-OBL-005 (PG 컬럼 물리 존치) → Task 7 (ALTER 금지 명시) ✓
- 설계 §3.3 SPEC retire/amend → Task 10 ✓
- 설계 §5 회귀(전체 pytest/ruff/import 스윕/link_resolver) → Task 9·11 ✓

**2. Placeholder scan:** 코드 스텝 전부 구체 코드 포함. 삭제 스텝은 정확 파일목록 + grep 검증 명령 포함. "적절히 처리" 류 없음. ✓

**3. Type consistency:** `maybe_first_touch(state, sess, adapter, *, taste_delete=None, breadcrumbs)` — Task 2 정의/Task 2 Step 5 호출 시그니처 일치. `is_reset_keyword`/`RESET_KEYWORDS` — Task 1 정의, Task 2·3 사용 일치. `_route_after_ingest_v2` 모듈 노출 seam — Task 3 Step 3(e) 정의, Step 1 테스트 사용 일치. `onboarded_at` 필드 — Task 7에서 유일 존치, Task 2/3에서 참조 일치. ✓

---

## Execution Handoff

이 계획은 사용자가 "같이 테스트하면서 하나씩" 진행을 원하므로 **Inline Execution (executing-plans)** + 태스크 경계 체크포인트 권장. 사용자 선택 대기.
