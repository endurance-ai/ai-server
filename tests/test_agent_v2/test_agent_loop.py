"""SPEC-AGENT-V2-REACT — react_loop core engine tests.

Mocks LLM via langchain message stubs. Verifies:
- Iteration cap (REQ-AGENT-LOOP-ITERATION-001)
- Termination on respond (REQ-AGENT-LOOP-TERMINATION-001)
- Exhaustion fallback (REQ-AGENT-LOOP-EXHAUSTION-001)
- Tool exception caught (REQ-AGENT-FAILURE-TOOL-001)
- Infinite-loop guard (REQ-AGENT-FAILURE-INFINITE-001)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.channels.schemas import ChannelMessage
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import Session, SessionState


class _FakeAIMessage:
    def __init__(self, tool_calls, content=""):
        self.tool_calls = tool_calls
        self.content = content
        self.usage_metadata = {"total_tokens": 100}


class _FakeLLM:
    """LLM stub. Each `ainvoke` returns the next pre-canned AIMessage."""

    def __init__(self, responses):
        self._responses = list(responses)

    async def ainvoke(self, messages):
        if not self._responses:
            return _FakeAIMessage([])
        return self._responses.pop(0)


def _make_state(text: str = "hello") -> WorkingState:
    from datetime import UTC, datetime

    msg = ChannelMessage(chat_id=42, text=text, received_at=datetime.now(UTC))
    return WorkingState(message=msg, chat_id=42, from_user_id=99)


def _make_session() -> Session:
    return Session(chat_id=42, state=SessionState.IDLE)


@pytest.mark.asyncio
async def test_respond_tool_terminates(monkeypatch):
    from app.agents import react_loop as rl

    fake = _FakeLLM([_FakeAIMessage([{"name": "respond", "args": {"text": "hi"}, "id": "1"}])])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    # Mock adapter for the respond tool dispatcher.
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    state = _make_state()
    sess = _make_session()
    delta = await rl.run_react_loop(state, sess)
    assert delta["agent_status"] == "done"
    assert delta["agent_iterations"] == 1
    mock_adapter.send_text.assert_awaited()


@pytest.mark.asyncio
async def test_iteration_cap_exhausts(monkeypatch):
    from app.agents import react_loop as rl

    # 7 responses of refine_search → never terminates → exhaustion.
    fake = _FakeLLM(
        [_FakeAIMessage([{"name": "refine_search", "args": {"action": "broaden"}, "id": str(i)}]) for i in range(10)]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(
        "app.agents.tools.refine_search.dispatch",
        AsyncMock(
            return_value={
                "ok": False,
                "error": "missing_image_url_in_ctx",
                "candidates_count": 0,
                "top_candidates": [],
            }
        ),
    )
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_MAX_ITERATIONS", 3, raising=False)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"
    assert delta["agent_iterations"] == 3


@pytest.mark.asyncio
async def test_fail_closed_when_llm_missing(monkeypatch):
    from app.agents import react_loop as rl

    monkeypatch.setattr(rl, "get_llm", lambda: None)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"
    assert delta["response_text"]  # fallback text emitted


@pytest.mark.asyncio
async def test_infinite_loop_guard(monkeypatch):
    """3 consecutive identical (tool_name, args) → exhausted."""
    from app.agents import react_loop as rl

    same_call = {"name": "get_recent_history", "args": {"n": 5}, "id": "x"}
    fake = _FakeLLM([_FakeAIMessage([same_call]) for _ in range(6)])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"


@pytest.mark.asyncio
async def test_json_malform_no_tool_calls(monkeypatch):
    from app.agents import react_loop as rl

    # Two truly empty responses (no tool_calls AND no meaningful text)
    # → exhaustion (2-strike malform).
    fake = _FakeLLM([_FakeAIMessage([], content=""), _FakeAIMessage([], content="")])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"


@pytest.mark.asyncio
async def test_no_tool_call_but_text_synthesizes_respond(monkeypatch):
    """LLM writes a closing text but forgets the `respond` tool_call wrapper.

    Observed 2026-07-06 trace 7023bf36 ("카프리팬츠 찾아줘"): iter 2's
    search_products returned 13 candidates → iter 3 LLM wrote "오 카프리팬츠
    몇 개 골라봤어! ..." as plain content (no tool_calls). Prior behavior
    discarded the text and nudged for another iteration; the fix routes the
    text through a synthetic `respond` dispatch so the loop terminates with
    the user actually seeing the answer.
    """
    from app.agents import react_loop as rl

    closing_text = "오 카프리팬츠 몇 개 골라봤어! 다양하게 있네 ✨"
    fake = _FakeLLM([_FakeAIMessage([], content=closing_text)])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "done"
    assert delta["agent_iterations"] == 1
    # The synthesized respond ran the real dispatcher, which sent the LLM's
    # own text to the adapter (not a fallback "잠깐만..." message).
    mock_adapter.send_text.assert_awaited_once()
    sent_text = mock_adapter.send_text.await_args.args[1]
    assert sent_text.startswith("오 카프리팬츠")
    # History records the synthesis so we can measure how often this fires.
    hist = delta["tool_call_history"]
    assert hist and hist[-1]["tool_name"] == "respond"


@pytest.mark.asyncio
async def test_no_tool_call_anthropic_content_blocks(monkeypatch):
    """Anthropic returns `content` as a list of typed blocks (thinking + text)
    on tool-use turns. The extractor must ignore non-text blocks and stitch
    together only the visible text.
    """
    from app.agents import react_loop as rl

    blocks = [
        {"type": "thinking", "thinking": "internal deliberation..."},
        {"type": "text", "text": "여기 세 벌 골라봤어! "},
        {"type": "text", "text": "취향에 맞는 게 있으면 알려줘 🐱"},
    ]
    fake = _FakeLLM([_FakeAIMessage([], content=blocks)])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "done"
    sent_text = mock_adapter.send_text.await_args.args[1]
    # Both text blocks concatenated; thinking block is not leaked.
    assert "세 벌 골라봤어" in sent_text
    assert "취향에 맞는 게" in sent_text
    assert "internal deliberation" not in sent_text


@pytest.mark.asyncio
async def test_no_tool_call_whitespace_only_still_nudges(monkeypatch):
    """A whitespace-only or trivially short content is NOT worth synthesizing
    a respond call — the malform-streak safety net must still fire so we don't
    send empty text to the user.
    """
    from app.agents import react_loop as rl

    # Both attempts have blank content → streak reaches 2 → exhaustion path.
    fake = _FakeLLM(
        [
            _FakeAIMessage([], content="   \n  "),
            _FakeAIMessage([], content=""),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"


@pytest.mark.asyncio
async def test_assistant_tooluse_precedes_toolmessage_on_2nd_ainvoke(monkeypatch):
    """Regression guard for the Bedrock Nova multi-turn 400.

    Before the fix, the assistant tool_use AIMessage was never appended to
    `messages`, so the 2nd ainvoke saw [system, user, tool_result] with a
    tool_result that had no preceding assistant tool_use turn. Bedrock Nova
    (via LiteLLM) 400s: "Expected toolResult blocks ... for the following Ids".

    This test asserts that, at the point of the 2nd ainvoke, `messages`
    contains the assistant AIMessage (carrying tool_calls) IMMEDIATELY
    followed by a ToolMessage whose tool_call_id equals the tool call's id.
    """
    from langchain_core.messages import ToolMessage

    from app.agents import react_loop as rl

    captured: list[list] = []

    class _SpyLLM:
        def __init__(self, responses):
            self._responses = list(responses)

        async def ainvoke(self, messages):
            captured.append(list(messages))
            return self._responses.pop(0) if self._responses else _FakeAIMessage([])

    first = _FakeAIMessage([{"name": "get_recent_history", "args": {"n": 5}, "id": "tooluse_ABC"}])
    second = _FakeAIMessage([{"name": "respond", "args": {"text": "hi"}, "id": "tooluse_DEF"}])
    fake = _SpyLLM([first, second])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(
        "app.agents.tools.get_recent_history.dispatch",
        AsyncMock(return_value={"ok": True, "events": []}),
    )
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "done"

    # The 2nd ainvoke's message list is what Bedrock would have rejected.
    assert len(captured) >= 2, "expected a 2nd ainvoke (multi-step turn)"
    msgs_2nd = captured[1]
    # Find the assistant tool_use turn (the raw ai_msg, carrying tool_calls)
    # appended before its tool result. In production this is a langchain
    # AIMessage; the fake mirrors its duck-typed shape (.tool_calls).
    ai_idx = next(
        i for i, m in enumerate(msgs_2nd) if not isinstance(m, (dict, ToolMessage)) and getattr(m, "tool_calls", None)
    )
    nxt = msgs_2nd[ai_idx + 1]
    assert isinstance(nxt, ToolMessage), "ToolMessage must immediately follow assistant tool_use"
    tc_id = first.tool_calls[0]["id"]
    assert nxt.tool_call_id == tc_id, (
        f"tool_call_id must match the tool call id exactly (got {nxt.tool_call_id!r}, expected {tc_id!r})"
    )


@pytest.mark.asyncio
async def test_invalid_args_recorded_not_dispatched(monkeypatch):
    from app.agents import react_loop as rl

    bad_then_respond = [
        _FakeAIMessage([{"name": "respond", "args": {"bogus_field": True}, "id": "1"}]),
        _FakeAIMessage([{"name": "respond", "args": {"text": "hi"}, "id": "2"}]),
    ]
    fake = _FakeLLM(bad_then_respond)
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    # Bad iter recorded, second iter succeeded.
    assert delta["agent_status"] == "done"
    assert delta["agent_iterations"] == 2
    assert any("invalid_args" in (h.get("error") or "") for h in delta["tool_call_history"])


@pytest.mark.asyncio
async def test_build_user_message_includes_picked_item():
    """SPEC-AGENT-V2-REACT regression guard.

    When an item is pre-selected from a photo pick, `_build_user_message`
    MUST surface the item label + a search query (else the LLM hallucinates a
    "can't recall the conversation" fallback — the original root bug).
    """
    from datetime import UTC, datetime

    from app.agents import react_loop as rl

    items = [
        {
            "label": "relaxed dark brown wool blazer",
            "subcategory": "blazer",
            "fit": "relaxed",
            "colorFamily": "BROWN",
            "searchQuery": "relaxed dark brown wool blazer",
            "searchQueryKo": "릴렉스드 다크 브라운 울 블레이저",
        },
        {"label": "denim shirt", "searchQueryKo": "데님 셔츠"},
    ]
    msg = ChannelMessage(chat_id=42, callback_data="item:0", received_at=datetime.now(UTC))
    state = WorkingState(
        message=msg,
        chat_id=42,
        from_user_id=99,
        image_url="https://example.com/p.jpg",
        detected_items=items,
        selected_item_index=0,
    )
    sess = Session(chat_id=42, state=SessionState.AWAITING_INTENT)
    sess.lang = "ko"

    out = rl._build_user_message(state, sess)
    assert "relaxed dark brown wool blazer" in out
    # 260522 (SPEC-GENDER-PIN-001): suggested_query is now ALWAYS the English
    # searchQuery (text_query is English-only) so the LLM never has to translate
    # the Korean variant — which was losing/flipping the gender token. KO
    # searchQueryKo is no longer surfaced as suggested_query.
    assert "릴렉스드 다크 브라운 울 블레이저" not in out
    assert 'suggested_query: "relaxed dark brown wool blazer"' in out
    assert "user_selected_item:" in out
    assert "suggested_query:" in out
    # Opaque raw callback must be replaced by the resolved description.
    assert "callback: item:0" not in out


@pytest.mark.asyncio
async def test_build_user_message_handles_out_of_range_index():
    """No IndexError when selected_item_index is stale / out of range."""
    from datetime import UTC, datetime

    from app.agents import react_loop as rl

    msg = ChannelMessage(chat_id=42, callback_data="item:5", received_at=datetime.now(UTC))
    state = WorkingState(
        message=msg,
        chat_id=42,
        from_user_id=99,
        detected_items=[{"label": "shirt"}],
        selected_item_index=5,
    )
    sess = Session(chat_id=42, state=SessionState.IDLE)
    out = rl._build_user_message(state, sess)
    # Falls back to detected_items summary, raw callback preserved (no crash).
    assert "detected_items:" in out
    assert "callback: item:5" in out


# --- 260612 direct photo upload surfacing — friendly link-nudge framing ---


def test_direct_photo_upload_surfaces_link_nudge_to_llm():
    """REGRESSION 260612 — a user-uploaded photo (`photo_file_id`
    without a `urls` link) used to vanish into thin air: `resolve_image`
    skipped the file path, leaving `state.image_url=None`, and the agent saw
    an empty user_text so it greeted as if nothing was sent. Now the LLM is
    given a friendly directive that frames link sharing as the BETTER
    analysis path (not a "I can't see it" rejection).
    """
    from datetime import UTC, datetime

    from app.agents import react_loop as rl

    msg = ChannelMessage(chat_id=42, photo_file_id="AgACAgIAAxkBAAEB", received_at=datetime.now(UTC))
    state = WorkingState(
        message=msg,
        chat_id=42,
        from_user_id=99,
        image_url=None,  # resolve_image left it empty (direct upload unsupported).
    )
    sess = Session(chat_id=42, state=SessionState.IDLE)
    sess.lang = "ko"

    out = rl._build_user_message(state, sess)
    assert "user_uploaded_direct_photo" in out
    # Tone guard — must NOT phrase it as a deficiency.
    assert "cannot" in out  # the directive says "do NOT say you cannot" — text present in the directive
    assert "more accurately" in out or "더 정확히" in out
    # Acknowledgment cue present so the LLM doesn't ignore the photo.
    assert "Acknowledge" in out


# --- 260611 crit:{op}:{idx} anchor surfacing — preserve clicked card color/style ---


def test_anchor_card_line_surfaces_brand_name_price_for_crit_cheap():
    """REGRESSION 260611 — clicking `💰 더 저렴한 거` on a specific card surfaced
    only the raw `callback: crit:cheap:3` to the LLM, so the LLM dropped the
    anchor's distinctive color/style (e.g. white sleeveless → mixed colors).
    Now the anchor's brand/name/price + a directive to preserve color/style
    in `refine_search.boost_keywords` is appended.
    """
    from app.agents import react_loop as rl

    sess = type(
        "Sess",
        (),
        {
            "last_results": [
                type("C", (), {"name": "Slim Tee", "brand": "ZARA", "price": 39000})(),
                type("C", (), {"name": "Linen Pants", "brand": "Acne", "price": 71000})(),
                type("C", (), {"name": "Vintage Tank", "brand": "Versace", "price": 128000})(),
                type("C", (), {"name": "WHITE SLEEVELESS BLOUSE", "brand": "DIVE IN", "price": 71357})(),
            ],
        },
    )()
    line = rl._anchor_card_line("crit:cheap:3", sess)
    assert line is not None
    assert "op=cheap" in line
    assert "WHITE SLEEVELESS BLOUSE" in line
    assert "DIVE IN" in line
    assert "₩71,357" in line
    # Directive must explicitly tell the LLM to preserve color/style words.
    assert "boost_keywords" in line
    assert "white" in line  # explicit color hint in directive


def test_anchor_card_line_returns_none_for_non_crit_callbacks():
    from app.agents import react_loop as rl

    sess = type("Sess", (), {"last_results": [type("C", (), {"name": "x"})()]})()
    assert rl._anchor_card_line("card:like:p1", sess) is None
    assert rl._anchor_card_line("cards:more", sess) is None
    assert rl._anchor_card_line("clarify:gender:women", sess) is None
    assert rl._anchor_card_line(None, sess) is None


def test_anchor_card_line_returns_none_when_idx_out_of_range():
    from app.agents import react_loop as rl

    sess = type("Sess", (), {"last_results": [type("C", (), {"name": "x"})()]})()
    assert rl._anchor_card_line("crit:cheap:5", sess) is None


# --- 260610 STALE-LEAK FIX — prior outfit context leaks into fresh turns ---


def test_fresh_garment_search_does_not_trigger_followup_leak():
    """REGRESSION 260610 — garment nouns ("셔츠", "코트" etc.) were in
    `_FOLLOWUP_TOKENS_KO`, causing fresh searches like "그레이 셔츠 추천해줘"
    to be misclassified as follow-up references. The prior outfit (e.g. a
    red T-shirt searched earlier) would leak as `previously_selected_item`
    / `detected_items` into the LLM context, contaminating the new search.
    """
    import time
    from datetime import UTC, datetime

    from app.agents import react_loop as rl

    prior_items = [
        {"label": "red short sleeve t-shirt", "searchQuery": "red short sleeve t-shirt"},
    ]
    # Recent session with prior Vision items stashed at session level.
    msg = ChannelMessage(chat_id=42, text="그레이 셔츠 추천해줘", received_at=datetime.now(UTC))
    state = WorkingState(
        message=msg,
        chat_id=42,
        from_user_id=99,
        # NO new image this turn, NO new detected_items — pure fresh text.
    )
    sess = Session(chat_id=42, state=SessionState.IDLE)
    sess.lang = "ko"
    sess.detected_items = prior_items
    sess.vision_selected_item_index = 0
    sess.last_active = time.time()  # within follow-up recency window

    out = rl._build_user_message(state, sess)
    # Fresh garment search must NOT carry the prior outfit context.
    assert "red short sleeve t-shirt" not in out
    assert "previously_selected_item" not in out
    assert "detected_items" not in out


def test_fresh_image_turn_does_not_fall_back_to_session_items():
    """REGRESSION 260610 — when a NEW image arrives but vision_node yielded
    no `state.detected_items` (e.g. transient Vision failure), the legacy
    `_detected_items` combined view fell back to `sess.detected_items` from
    the prior turn. With `state.image_url` set, the L653 branch then surfaced
    the OLD items as if they belonged to the new image. Fresh image turn must
    surface ONLY state-level items (None → no leak)."""
    from datetime import UTC, datetime

    from app.agents import react_loop as rl

    stale_items = [{"label": "navy baseball cap", "searchQuery": "navy baseball cap"}]
    msg = ChannelMessage(chat_id=42, received_at=datetime.now(UTC))
    state = WorkingState(
        message=msg,
        chat_id=42,
        from_user_id=99,
        image_url="https://example.com/new-photo.jpg",
        # Vision failed THIS turn — state.detected_items empty.
    )
    sess = Session(chat_id=42, state=SessionState.IDLE)
    sess.lang = "ko"
    sess.detected_items = stale_items

    out = rl._build_user_message(state, sess)
    # The OLD cap must NOT surface just because state.image_url is set.
    assert "navy baseball cap" not in out


def test_followup_marker_still_works_for_genuine_refinement():
    """Positive guard — '더 저렴하게' / '다른 색상' WITH prior context still
    trigger surfacing. Only the over-broad garment / pure-image markers were
    removed; refinement intent must remain detectable."""
    import time
    from datetime import UTC, datetime

    from app.agents import react_loop as rl

    prior_items = [
        {
            "label": "minimal beige trench coat",
            "subcategory": "coat",
            "searchQuery": "minimal beige trench coat",
        },
    ]
    msg = ChannelMessage(chat_id=42, text="더 저렴한 거 있을까", received_at=datetime.now(UTC))
    state = WorkingState(
        message=msg,
        chat_id=42,
        from_user_id=99,
    )
    sess = Session(chat_id=42, state=SessionState.IDLE)
    sess.lang = "ko"
    sess.detected_items = prior_items
    sess.vision_selected_item_index = 0
    sess.last_active = time.time()

    out = rl._build_user_message(state, sess)
    # Genuine refinement → prior context surfaced.
    assert "minimal beige trench coat" in out
    assert "previously_selected_item" in out


# --- SPEC-AGENT-V2-REACT runtime hardening: Fix A + Fix B ---


def test_system_prompt_contains_redundancy_rules():
    """Fix A + B13 — react_loop _SYSTEM_PROMPT must carry the redundancy rules
    AND the B13 exception/anti-lie guards (new signal → refine, never promise
    new results in respond text without actually calling search/refine)."""
    from app.agents import react_loop as rl

    p = rl._SYSTEM_PROMPT
    # anti-redundancy (moved to the fewest-calls rule; G1b clarifies re-show is NOT redundant)
    assert "NEVER repeat a tool with identical args" in p
    # G1 greeting/chit-chat → plain respond (no search)
    assert "greeting" in p and "chit-chat" in p
    # card-honesty contract: cards only on a turn that searched; never point off-screen
    assert "ZERO cards are shown" in p
    assert "스크롤" in p  # banned scroll-up phrasing is explicitly called out
    assert "G1b" in p  # want-to-see/pick → must search this turn
    assert "Do NOT call `analyze_image`" in p
    # vision-context guard
    assert "`detected_items:`" in p and "`user_selected_item:`" in p and "`style_node:`" in p
    # fewest-calls rule
    assert "Prefer the fewest tool calls" in p
    # B13: new-signal exception forces refine_search rather than respond-only
    assert "EXCEPTION" in p and "refine_search" in p
    assert "Sportmax" in p or "사이드" in p or "new signal" in p  # concrete trigger example
    # B13: anti-lie rule
    assert "ANTI-LIE RULE" in p
    assert "골라봤어" in p or "찾아봤어" in p or "found some" in p  # promise-keyword example
    # additive — original persona / fence rules still present
    assert "You are kiko" in p
    assert "ReAct agent" in p


class _FakeHTTPError(Exception):
    """Mimics httpx/openai status-bearing exception."""

    def __init__(self, status_code: int, msg: str = ""):
        super().__init__(msg or f"HTTP {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_FakeHTTPError(500), True),
        (_FakeHTTPError(502), True),
        (_FakeHTTPError(503), True),
        (_FakeHTTPError(504), True),
        (_FakeHTTPError(400), False),
        (_FakeHTTPError(401), False),
        (_FakeHTTPError(422), False),
        (_FakeHTTPError(404), False),
        (TimeoutError("read timeout"), True),
        (Exception("litellm.InternalServerError: Internal Server Error"), True),
        (Exception("Bedrock throttlingException: rate exceeded"), True),
        (Exception("503 Service Unavailable"), True),
        (Exception("connect timeout"), True),
        (Exception("400 Bad Request: invalid model"), False),
        (Exception("ValidationError: missing field"), False),
        (ValueError("some random bug"), False),
    ],
)
def test_is_transient_truth_table(exc, expected):
    from app.agents import react_loop as rl

    assert rl._is_transient(exc) is expected


class _FlakyLLM:
    """Raises a given exc N times, then returns canned messages."""

    def __init__(self, exc, fail_times, then):
        self._exc = exc
        self._fail = fail_times
        self._then = list(then)
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self._fail > 0:
            self._fail -= 1
            raise self._exc
        if self._then:
            return self._then.pop(0)
        return _FakeAIMessage([])


@pytest.mark.asyncio
async def test_llm_retry_then_success(monkeypatch):
    """Fix B — one transient 500 then success → loop recovers, NOT exhausted."""
    from app.agents import react_loop as rl

    flaky = _FlakyLLM(
        _FakeHTTPError(500, "Internal Server Error"),
        fail_times=1,
        then=[_FakeAIMessage([{"name": "respond", "args": {"text": "ok"}, "id": "1"}])],
    )
    monkeypatch.setattr(rl, "get_llm", lambda: flaky)
    monkeypatch.setattr(rl, "_LLM_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "done"
    assert delta["agent_iterations"] == 1  # retry did NOT consume an iteration
    assert flaky.calls == 2  # 1 fail + 1 success
    mock_adapter.send_text.assert_awaited()


@pytest.mark.asyncio
async def test_llm_retry_exhausted_falls_back(monkeypatch):
    """Fix B — 3 consecutive 500s (> max 2 retries) → graceful single fallback."""
    from app.agents import react_loop as rl

    flaky = _FlakyLLM(_FakeHTTPError(500), fail_times=10, then=[])
    monkeypatch.setattr(rl, "get_llm", lambda: flaky)
    monkeypatch.setattr(rl, "_LLM_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"
    assert delta["response_text"]  # ONE user-facing message
    # 1 initial + 2 retries = 3 calls, then fallback (no infinite loop)
    assert flaky.calls == 3


@pytest.mark.asyncio
async def test_llm_non_transient_no_retry(monkeypatch):
    """Fix B — a 400 must NOT retry (fast fail to fallback), behavior unchanged."""
    from app.agents import react_loop as rl

    flaky = _FlakyLLM(_FakeHTTPError(400, "Bad Request"), fail_times=10, then=[])
    monkeypatch.setattr(rl, "get_llm", lambda: flaky)
    monkeypatch.setattr(rl, "_LLM_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "exhausted"
    assert flaky.calls == 1  # NO retry on non-transient


@pytest.mark.asyncio
async def test_tool_dispatch_transient_retry(monkeypatch):
    """Fix B — tool dispatch transient 5xx retried once then succeeds."""
    from app.agents import react_loop as rl

    # LLM: call search_products, then respond.
    fake = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "navy jeans"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "here you go"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(rl, "_LLM_BACKOFF", (0.0, 0.0))
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))

    calls = {"n": 0}

    async def _flaky_dispatch(args, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeHTTPError(503, "Service Unavailable")
        return {"ok": True, "candidates_count": 5}

    _real_resolve = rl._resolve_dispatcher

    def _resolve(name):
        # Only the search tool is flaky; respond uses the real dispatcher.
        return _flaky_dispatch if name == "search_products" else _real_resolve(name)

    monkeypatch.setattr(rl, "_resolve_dispatcher", _resolve)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "done"
    assert calls["n"] == 2  # 1 transient fail + 1 retry success
    hist = delta["tool_call_history"]
    assert hist[0]["tool_name"] == "search_products"
    assert hist[0]["error"] is None  # retry cleared the error


# ---------------------------------------------------------------------------
# SPEC-AGENT-V2-REACT — terminal `respond` is NEVER retried (double-send fix).
# ---------------------------------------------------------------------------


class _SlowCardAdapter:
    """Adapter whose send_card sleeps so N cards exceed the per-tool timeout."""

    def __init__(self, per_card_s: float):
        self._per_card_s = per_card_s
        self.text_calls = 0
        self.card_calls = 0
        # 260610 — Shop URL row was removed (button_url is now None); track
        # `image_url` instead for uniqueness assertions (each candidate has a
        # distinct product image).
        self.card_image_urls: list[str | None] = []

    async def send_text(self, chat_id, text):
        self.text_calls += 1
        return "tmid"

    async def send_card(self, chat_id, card):
        import asyncio as _a

        await _a.sleep(self._per_card_s)
        self.card_calls += 1
        self.card_image_urls.append(str(card.image_url) if card.image_url else None)
        return f"mid{self.card_calls}"


def _cand(i: int):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=f"p{i}",
        image_url=f"https://img/{i}.jpg",
        product_url=f"https://shop/{i}",
        brand="Acme",
        name=f"Item {i}",
        platform="Acme",
        subcategory="shoes",
        price=10000 + i,
    )


@pytest.mark.asyncio
async def test_respond_not_retried_on_slow_card_timeout(monkeypatch):
    """Slow 4-card carousel that would exceed AGENT_TOOL_TIMEOUT_S must NOT
    trigger a respond retry: text sent once, each card once, status=done."""
    # Force album mode for this legacy assertion (SPEC-CARD-DELIVERY-001).
    monkeypatch.setattr("app.core.config.settings.INDIVIDUAL_CARD_DELIVERY", False)
    from app.agents import react_loop as rl
    from app.infrastructure.memory.session import get_store

    sess = _make_session()
    store = get_store()
    store_sess = store.get_or_create(42)
    store_sess.lang = "en"

    # A real search runs THIS turn (mocked pipeline → 4 candidates). This
    # populates last_results AND sets the per-turn card-ready marker via the
    # genuine persist_last_results path — the precondition for respond to send
    # cards at all (cards are gated on a per-turn search, not on stale
    # last_results non-emptiness).
    async def _fake_search_step(state):
        state.raw_candidates = [
            {
                "id": f"p{i}",
                "name": f"Item {i}",
                "brand": "Acme",
                "price": 10000 + i,
                "image_url": f"https://img/{i}.jpg",
                "product_url": f"https://shop/{i}",
            }
            for i in range(4)
        ]
        return state

    async def _fake_diversify_step(state):
        state.final_candidates = list(state.raw_candidates)
        return state

    monkeypatch.setattr("app.pipeline.search.search_step", _fake_search_step)
    monkeypatch.setattr("app.pipeline.diversify.diversify_step", _fake_diversify_step)
    # This is the only test in this file using the REAL dispatcher + a
    # search_step-level mock (others patch _resolve_dispatcher), so it reaches
    # the genuine v6 run_text_only_search → EmbedProvider.embed_text. Patch it
    # (same app.pipeline.embed seam as embed_image_url) so no Modal call fires
    # offline in CI ('Event loop is closed'). v6 test-seam re-point.
    monkeypatch.setattr(
        "app.pipeline.embed.EmbedProvider.embed_text",
        AsyncMock(return_value=[0.1] * 768),
    )

    # 260522 (SPEC-GENDER-PIN-001): an explicit gender token in text_query
    # satisfies the gender gate (per-request override) so this respond-retry
    # test reaches the search/card path instead of the one-time gender card.
    fake = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "boots men"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "here you go"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))
    # Force the generic per-tool timeout tiny so 4×0.05s cards blow past it,
    # but respond gets its dedicated (generous) AGENT_RESPOND_TIMEOUT_S.
    monkeypatch.setattr(rl.settings, "AGENT_TOOL_TIMEOUT_S", 0.01)
    monkeypatch.setattr(rl.settings, "AGENT_RESPOND_TIMEOUT_S", 30.0)

    # Isolate from real Redis impression-dedup state (260611 cross-turn dedup).
    # Without this, prior test-run products in kiko:imp:42 filter out all 4
    # candidates as "already shown", delivering 0 cards instead of 4.
    monkeypatch.setattr("app.infrastructure.cache.chat_state.is_logged", AsyncMock(return_value=False))
    monkeypatch.setattr("app.infrastructure.cache.chat_state.mark_logged", AsyncMock())
    monkeypatch.setattr("app.infrastructure.cache.chat_state.get_cursor", AsyncMock(return_value=0))
    monkeypatch.setattr("app.infrastructure.cache.chat_state.set_cursor", AsyncMock())

    adapter = _SlowCardAdapter(per_card_s=0.05)
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

    delta = await rl.run_react_loop(_make_state(), sess)

    assert delta["agent_status"] == "done"
    assert adapter.text_calls == 1  # text sent EXACTLY once (no retry resend)
    assert adapter.card_calls == 4  # each card sent EXACTLY once
    # No duplicate cards — distinct product image URLs (Shop URL row removed
    # 260610 so button_url is None on every card; image_url is the new
    # per-candidate uniqueness signal).
    assert len(set(adapter.card_image_urls)) == 4
    respond_hist = [h for h in delta["tool_call_history"] if h["tool_name"] == "respond"]
    assert len(respond_hist) == 1  # respond dispatched once — NOT retried


@pytest.mark.asyncio
async def test_respond_transient_before_send_not_retried(monkeypatch):
    """A real transient error (TimeoutError) from the terminal respond tool
    before any send must NOT be retried — loop ends gracefully (done)."""
    from app.agents import react_loop as rl

    fake = _FakeLLM([_FakeAIMessage([{"name": "respond", "args": {"text": "hi"}, "id": "1"}])])
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))

    calls = {"n": 0}

    async def _boom(args, ctx):
        calls["n"] += 1
        raise TimeoutError()

    monkeypatch.setattr(rl, "_resolve_dispatcher", lambda name: _boom)
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())

    assert calls["n"] == 1  # terminal tool NOT retried even on transient
    assert delta["agent_status"] == "done"  # terminal tool always terminates


@pytest.mark.asyncio
async def test_non_terminal_tool_still_retries_on_transient(monkeypatch):
    """Regression guard: the earlier transient-retry feature for NON-terminal
    tools must still work (only `respond` is excluded)."""
    from app.agents import react_loop as rl

    fake = _FakeLLM(
        [
            _FakeAIMessage([{"name": "search_products", "args": {"text_query": "boots"}, "id": "1"}]),
            _FakeAIMessage([{"name": "respond", "args": {"text": "done"}, "id": "2"}]),
        ]
    )
    monkeypatch.setattr(rl, "get_llm", lambda: fake)
    monkeypatch.setattr(rl, "_TOOL_BACKOFF", (0.0,))

    calls = {"n": 0}

    async def _flaky(args, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeHTTPError(503, "Service Unavailable")
        return {"ok": True, "candidates_count": 3}

    _real = rl._resolve_dispatcher
    monkeypatch.setattr(rl, "_resolve_dispatcher", lambda name: _flaky if name == "search_products" else _real(name))
    mock_adapter = MagicMock()
    mock_adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: mock_adapter)

    delta = await rl.run_react_loop(_make_state(), _make_session())
    assert delta["agent_status"] == "done"
    assert calls["n"] == 2  # non-terminal tool DID retry (1 fail + 1 success)


@pytest.mark.asyncio
async def test_respond_self_idempotent_on_double_call(monkeypatch):
    """respond.dispatch is self-idempotent: a second call with the same ctx
    re-sends neither text nor already-sent cards."""
    # Force album mode for this legacy assertion (SPEC-CARD-DELIVERY-001).
    monkeypatch.setattr("app.core.config.settings.INDIVIDUAL_CARD_DELIVERY", False)
    from app.agents.tools.respond import dispatch as respond_dispatch
    from app.agents.tools.search_products import CARDS_READY_KEY
    from app.infrastructure.memory.session import get_store

    store = get_store()
    sess = store.get_or_create(77)
    sess.last_results = [_cand(0), _cand(1)]
    sess.lang = "en"

    counts = {"text": 0, "card": 0}

    class _A:
        async def send_text(self, c, t):
            counts["text"] += 1
            return "t"

        async def send_card(self, c, card):
            counts["card"] += 1
            return f"m{counts['card']}"

    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: _A())
    # Isolate from real Redis impression-dedup state (260611 cross-turn dedup).
    monkeypatch.setattr("app.infrastructure.cache.chat_state.is_logged", AsyncMock(return_value=False))
    monkeypatch.setattr("app.infrastructure.cache.chat_state.mark_logged", AsyncMock())
    monkeypatch.setattr("app.infrastructure.cache.chat_state.get_cursor", AsyncMock(return_value=0))
    monkeypatch.setattr("app.infrastructure.cache.chat_state.set_cursor", AsyncMock())

    # A search ran this turn (marker set by persist_last_results); the same
    # ctx is reused across the defensive 2nd entry (react_loop contract).
    ctx = {"chat_id": 77, "lang": "en", CARDS_READY_KEY: True}
    r1 = await respond_dispatch({"text": "hello"}, ctx)
    r2 = await respond_dispatch({"text": "hello"}, ctx)  # defensive 2nd entry

    assert r1["ok"] and r2["ok"]
    assert counts["text"] == 1  # text sent at most once
    assert counts["card"] == 2  # each card at most once (not 4)
