"""SPEC-AGENT-001 / REQ-MIGR-003 — integration tests through `GRAPH.ainvoke()`.

Covers REQ-COMPAT-001..009 and the 9 reachable terminal flows from
REQ-COMPAT-004.

Each test docstring references the REQ (or REQs) it covers.
"""

from __future__ import annotations

import pytest

from app.channels.recommendation import set_port
from app.core.config import settings
from app.graphs.fashion_bot import GRAPH
from app.graphs.nodes import respond as respond_module
from app.graphs.nodes._adapter_ctx import reset_adapter, set_adapter
from app.graphs.nodes.ask_clarify import _FALLBACK as ASK_CLARIFY_FALLBACK
from app.graphs.state import InputState
from app.infrastructure.memory.session import (
    InMemorySessionStore,
    SessionState,
    set_store,
    shutdown_store,
)
from app.infrastructure.memory.taste_profile import (
    InMemoryTasteProfileStore,
    set_taste_store,
    shutdown_taste_store,
)
from tests.conftest_graph import FakeAdapter, FakeCandidate, StubLLM, StubPort, make_msg

# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
async def store():
    """SPEC-ONBOARD-CARDS-001 cascade: graph_flows tests target legacy
    non-onboarding branches; pre-mark the canonical chat_id 42 as onboarded so
    the ingest gate's onboarding predicate stays off and existing flow assertions
    continue to fire (DDD PRESERVE for non-onboarding paths).
    """
    from datetime import UTC, datetime

    s = InMemorySessionStore()
    set_store(s)
    sess = s.get_or_create(42)
    sess.onboarded_at = datetime.now(tz=UTC)
    s.update(sess)
    yield s
    await shutdown_store()


@pytest.fixture
async def taste_store():
    s = InMemoryTasteProfileStore()
    set_taste_store(s)
    yield s
    await shutdown_taste_store()


@pytest.fixture
def stub_port():
    p = StubPort()
    set_port(p)
    yield p


@pytest.fixture
def adapter():
    a = FakeAdapter()
    token = set_adapter(a)
    yield a
    reset_adapter(token)


@pytest.fixture(autouse=True)
def disable_router_llm(monkeypatch):
    monkeypatch.setattr("app.channels.router.settings.ROUTER_LLM_ENABLED", False)


@pytest.fixture(autouse=True)
def stub_respond_llm(monkeypatch):
    """Inject a deterministic StubLLM into respond/ask_clarify so tests don't
    actually hit LiteLLM. The fallback strings still apply when StubLLM raises."""
    fake = StubLLM(content="Okay — sharing some matches now.")
    monkeypatch.setattr(respond_module, "_llm", fake)
    return fake


@pytest.fixture(autouse=True)
def _stub_route_text_default(monkeypatch):
    """Default router stub — returns CRITIQUE_TEXT to mimic the legacy direct
    routing (AWAITING_INTENT text → critique_apply). Individual tests can
    override this with their own `monkeypatch.setattr(...route_text...)` call
    (later setattr wins). Tests that don't care about router behavior get
    deterministic critique routing for free.
    """
    from app.channels.router import RoutedDecision, RoutedIntent

    async def _default_route(_text, _state, _last_results):
        return RoutedDecision(intent=RoutedIntent.CRITIQUE_TEXT)

    monkeypatch.setattr("app.channels.router.settings.ROUTER_LLM_ENABLED", True)
    monkeypatch.setattr("app.graphs.nodes.ingest.route_text", _default_route)


# SPEC-AGENT-V2-REACT / T-010 — under the V2 ReAct topology every
# post-onboarding text / critique-callback turn funnels through the single
# `agent` node (router_text passthrough, critique_apply, taste_update,
# send_results, respond, evaluator are NOT registered). The flows below assert
# V1 intermediate routing internals (stub_port wiring, V1 card/critique-button
# shapes, session result caching) that the agent loop replaces with autonomous
# LLM tool selection. With no LLM mock in this legacy harness the agent path
# is non-deterministic, so these tests have no V2 equivalent here and are
# skipped under flag=true (Bucket B2). The V2 equivalents are covered by
# tests/test_agent_v2/. Under flag=false they run unchanged (V1 coverage).
_v2_active = bool(settings.AGENT_V2_REACT_ENABLED and (settings.AGENT_LLM_MODEL or "").strip())
_skip_v1_flow = pytest.mark.skipif(
    _v2_active,
    reason="V1-only graph routing internals; superseded by SPEC-AGENT-V2-REACT agent loop "
    "(see tests/test_agent_v2/ for V2 coverage)",
)


def _state(message, **kw) -> InputState:
    return InputState(message=message, chat_id=message.chat_id, from_user_id=message.from_user_id, **kw)


async def _run(message) -> dict:
    return await GRAPH.ainvoke(_state(message))


# ── 9 reachable terminal flows (REQ-COMPAT-004) ────────────────────────────


@pytest.mark.asyncio
async def test_link_fail_routes_to_respond_link_fail_copy(store, taste_store, stub_port, adapter, monkeypatch):
    """Flow #1 — link-resolver failure → respond (LINK_FAIL fallback)."""
    msg = make_msg(urls=["https://www.pinterest.com/pin/zzz/"])
    import app.graphs.nodes.resolve_image as r

    async def _empty(_url):
        return []

    monkeypatch.setattr(r.link_resolver, "resolve", _empty)

    await _run(msg)
    # respond must have dispatched something.
    assert adapter.texts, "no silent dead-end"


@pytest.mark.asyncio
async def test_vision_fallback_routes_to_respond_zero_result(store, taste_store, stub_port, adapter, monkeypatch):
    """Flow #2/#3 — vision returns placeholder fallback → respond (VISION_FAIL).

    Mirrors the deleted scenario test of the same name.
    """
    msg = make_msg(urls=["https://www.pinterest.com/pin/123/"])
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_fallback(_url):
        return {"items": [{"label": "item", "description": "", "color": "", "keywords": []}]}

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_fallback)

    await _run(msg)
    assert adapter.texts
    assert stub_port.calls == []  # never reached search


@pytest.mark.asyncio
async def test_multi_item_sends_picker_and_ends(store, taste_store, stub_port, adapter, monkeypatch):
    """Flow #4 / REQ-AGENT-010 — picker sent, no respond fired."""
    msg = make_msg(urls=["https://www.pinterest.com/pin/123/"])
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_multi(_url):
        return {
            "items": [
                {"label": "white tee", "description": "round neck slim fit", "keywords": ["tee"]},
                {"label": "blue jeans", "description": "slim dark wash", "keywords": ["jeans"]},
            ]
        }

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_multi)

    await _run(msg)
    assert adapter.buttons, "picker carousel must be sent"
    # No respond dispatch — only the picker text.
    assert not adapter.texts, f"respond should NOT fire; got {adapter.texts}"
    assert store.get_or_create(42).state == SessionState.AWAITING_ITEM_PICK


@pytest.mark.asyncio
async def test_weak_vision_routes_to_ask_clarify(store, taste_store, stub_port, adapter, monkeypatch):
    """Flow #5 / REQ-AGENT-009 — weak/ambiguous vision does NOT blindly search;
    the bot responds asking the user for more (no silent dead-end).

    SPEC-AGENT-V2-REACT / T-010 (Bucket B1, flag-agnostic) — the V1
    implementation emits an inline-keyboard clarify card (SPEC-CLARIFY-CARDS-001);
    under V2 the same semantic guarantee holds via the agent (which either asks
    for clarification or falls back). Per REQ-AGENT-COMPAT-SEMANTIC-001 we
    assert the OUTPUT CLASS — some bot output is produced and search is NOT
    auto-triggered on weak vision — not the internal node sequence or card shape.
    """
    msg = make_msg(urls=["https://www.pinterest.com/pin/123/"])
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_weak(_url):
        # Single ambiguous label triggers _is_weak_vision.
        return {"items": [{"label": "item", "description": "kind of", "keywords": ["x"]}]}

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_weak)

    await _run(msg)
    # OUTPUT CLASS — bot produced *some* response (clarify card under V1, agent
    # reply/fallback under V2); never a silent dead-end.
    assert adapter.buttons or adapter.texts, (
        f"weak vision must produce a bot response; buttons={adapter.buttons} texts={adapter.texts}"
    )
    # Weak vision must NOT blindly run a product search this turn.
    assert not stub_port.calls, "weak vision must not auto-search before clarification"
    if not _v2_active:
        # V1-specific: SPEC-CLARIFY-CARDS-001 card shape (skip-terminated).
        chat_id, body, btns = adapter.buttons[0]
        assert chat_id == 42
        assert body, "card body must be non-empty"
        assert btns[-1][1].endswith(":skip"), f"last button must be skip; got {btns[-1]}"


@_skip_v1_flow
@pytest.mark.asyncio
async def test_weak_vision_legacy_fallback_when_flag_off(store, taste_store, stub_port, adapter, monkeypatch):
    """REQ-CLARIFY-COMPAT-002 — CLARIFY_CARDS_ENABLED=false 시 자유 텍스트 폴백.

    SPEC-AGENT-V2-REACT (Bucket B2) — asserts the V1 ask_clarify legacy
    free-text fallback (CLARIFY_CARDS_ENABLED feature-flag behavior). Under V2
    weak vision routes to `agent`; there is no ask_clarify legacy-fallback path,
    so this V1-specific feature-flag behavior has no V2 equivalent.
    """
    monkeypatch.setattr("app.graphs.nodes.ask_clarify.settings.CLARIFY_CARDS_ENABLED", False)
    msg = make_msg(urls=["https://www.pinterest.com/pin/123/"])
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_weak(_url):
        return {"items": [{"label": "item", "description": "kind of", "keywords": ["x"]}]}

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_weak)

    import app.graphs.nodes.ask_clarify as ac

    class _Boom:
        async def ainvoke(self, *a, **k):
            raise RuntimeError("stub")

    monkeypatch.setattr(ac, "_llm", _Boom())

    await _run(msg)
    assert adapter.texts == [(42, ASK_CLARIFY_FALLBACK)], (
        f"ask_clarify legacy fallback expected, got texts={adapter.texts} buttons={adapter.buttons}"
    )
    assert not adapter.buttons, "card path must NOT fire when flag is off"


@pytest.mark.asyncio
async def test_search_empty_routes_to_respond(store, taste_store, stub_port, adapter):
    """Flow #6 — an empty/unproductive search turn yields a bot text message
    and NO product-card carousel.

    SPEC-AGENT-V2-REACT / T-010 (Bucket B1, flag-agnostic) — under V1 empty
    search routes to the respond node; under V2 the agent (which, with no
    productive search result, replies in text or falls back) produces the same
    OUTPUT CLASS: a bot message, no cards. Per REQ-AGENT-COMPAT-SEMANTIC-001 we
    assert on observable output, not the internal node path or stub_port wiring.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    store.update(sess)
    stub_port.candidates = []

    msg = make_msg(text="something cheaper")
    await _run(msg)

    assert adapter.texts, "empty/unproductive turn must dispatch a bot message"
    assert not adapter.cards, "no cards on empty result"


@_skip_v1_flow
@pytest.mark.asyncio
async def test_search_with_results_full_path(store, taste_store, stub_port, adapter):
    """Flow #7 / REQ-COMPAT-001/005 — happy path: cards sent + respond closer.

    SPEC-AGENT-V2-REACT (Bucket B2) — the card-dispatch happy path is
    agent-tool-mediated under V2 (search_products + respond tools). Without an
    LLM mock the legacy harness cannot drive it deterministically; V2 coverage
    lives in tests/test_agent_v2/test_agent_loop.py.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    store.update(sess)

    msg = make_msg(text="something cheaper")
    await _run(msg)

    assert adapter.cards, "cards must be dispatched"
    assert adapter.texts, "respond closer must be dispatched"
    sess_after = store.get_or_create(42)
    assert sess_after.state == SessionState.RESULTS_SENT


@_skip_v1_flow
@pytest.mark.asyncio
async def test_taste_only_update_routes_to_respond_ack(store, taste_store, stub_port, adapter, monkeypatch):
    """Flow #8 / REQ-COMPAT-003 — explicit taste update via router.

    SPEC-AGENT-V2-REACT (Bucket B2) — V1 router TASTE_UPDATE → taste_update
    node. Under V2 taste mutation is the agent's `update_taste` tool decision;
    no deterministic equivalent without an LLM mock.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    store.update(sess)

    # Fake the router to return TASTE_UPDATE.
    from app.channels.router import RoutedDecision, RoutedIntent, TasteUpdate

    async def _stub_route(text, state, last_results):
        return RoutedDecision(intent=RoutedIntent.TASTE_UPDATE, taste_update=TasteUpdate(liked_brands=["ami"]))

    monkeypatch.setattr("app.channels.router.settings.ROUTER_LLM_ENABLED", True)
    monkeypatch.setattr("app.graphs.nodes.ingest.route_text", _stub_route)

    msg = make_msg(text="i love ami paris")
    await _run(msg)

    profile = taste_store.get_or_create("c:42")  # no from_user_id → c:{chat_id}
    assert "ami" in profile.liked_brands
    assert adapter.texts, "respond must ack"
    assert stub_port.calls == [], "taste-only update must not trigger search"


@pytest.mark.asyncio
async def test_off_topic_in_results_sent_routes_to_respond(store, taste_store, stub_port, adapter, monkeypatch):
    """Flow #9 / REQ-COMPAT-004 — off-topic text in RESULTS_SENT yields a reply.

    Regression test for the "silent dead end" bug PR #10 fixed.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "tee"
    store.update(sess)

    from app.channels.router import RoutedDecision, RoutedIntent

    async def _stub_route(text, state, last_results):
        return RoutedDecision(intent=RoutedIntent.OFF_TOPIC)

    monkeypatch.setattr("app.channels.router.settings.ROUTER_LLM_ENABLED", True)
    monkeypatch.setattr("app.graphs.nodes.ingest.route_text", _stub_route)

    await _run(make_msg(text="hello what's up"))
    assert adapter.texts, "no silent dead end"
    assert stub_port.calls == []


# ── REQ-COMPAT-001 — tap critique callbacks ────────────────────────────────


@_skip_v1_flow
@pytest.mark.asyncio
async def test_critique_tap_more_reinforces_taste_and_reruns(store, taste_store, stub_port, adapter):
    """REQ-COMPAT-001 + REQ-COMPAT-003 — `crit:more:0` callback path.

    SPEC-AGENT-V2-REACT (Bucket B2) — crit:* callbacks route to `agent` under
    V2; taste reinforcement + re-search is the agent's autonomous tool chain.
    """
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.last_results = [FakeCandidate(brand="ami", name="basic tee")]
    store.update(sess)

    msg = make_msg(callback_data="crit:more:0")
    await _run(msg)

    profile = taste_store.get_or_create("u:7")
    assert "ami" in profile.liked_brands
    assert stub_port.calls
    req = stub_port.calls[-1]
    assert "ami" in req.boost_brands


@_skip_v1_flow
@pytest.mark.asyncio
async def test_critique_tap_less_excludes_brand(store, taste_store, stub_port, adapter):
    """REQ-COMPAT-001 + REQ-COMPAT-006 — `crit:less:0` excludes brand + shown ids.

    SPEC-AGENT-V2-REACT (Bucket B2) — V1 critique-callback routing; superseded
    by the agent loop.
    """
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.last_results = [FakeCandidate(id="p1", brand="Zara")]
    sess.shown_product_ids = ["p1"]
    store.update(sess)

    await _run(make_msg(callback_data="crit:less:0"))

    profile = taste_store.get_or_create("u:7")
    assert "zara" in profile.disliked_brands
    req = stub_port.calls[-1]
    assert "zara" in req.exclude_brands
    assert "p1" in req.exclude_product_ids


@_skip_v1_flow
@pytest.mark.asyncio
async def test_critique_tap_cheap_sets_max_price(store, taste_store, stub_port, adapter):
    """REQ-COMPAT-001 / REQ-COMPAT-005 — `crit:cheap:0` sets max_price = price * 0.7.

    SPEC-AGENT-V2-REACT (Bucket B2) — V1 critique-callback routing; superseded
    by the agent loop.
    """
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "tee"
    sess.last_results = [FakeCandidate(brand="ami", price=100000)]
    store.update(sess)

    await _run(make_msg(callback_data="crit:cheap:0"))
    req = stub_port.calls[-1]
    assert req.max_price == 70000  # default ratio 0.7


@_skip_v1_flow
@pytest.mark.asyncio
async def test_critique_tap_invalid_idx_skips_search(store, taste_store, stub_port, adapter):
    """REQ-COMPAT-001 / REQ-AGENT-007 — stale callback skips search.

    SPEC-AGENT-V2-REACT (Bucket B2) — V1 stale-callback toast handled by
    critique_apply; under V2 crit:* routes to `agent`.
    """
    sess = store.get_or_create(42)
    sess.from_user_id = 7
    sess.state = SessionState.RESULTS_SENT
    sess.last_results = []
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "tee"
    store.update(sess)

    await _run(make_msg(callback_data="crit:more:9"))
    assert stub_port.calls == []
    assert any("out of date" in (t or "").lower() for _, t in adapter.callback_answers)


@_skip_v1_flow
@pytest.mark.asyncio
async def test_send_results_cards_carry_critique_buttons(store, taste_store, stub_port, adapter):
    """REQ-COMPAT-001 — every card carries crit:* buttons.

    SPEC-AGENT-V2-REACT (Bucket B2) — V1 send_results card shape (critique
    buttons). Under V2 cards are emitted by the agent `respond` tool; the
    crit:* button structure is a V1 send_results detail with no V2 equivalent.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "tee"
    store.update(sess)

    await _run(make_msg(text="for casual"))
    assert adapter.cards
    _, card = adapter.cards[0]
    assert card.critique_buttons
    labels = [lbl for lbl, _ in card.critique_buttons]
    assert any("More" in lb for lb in labels)
    assert any("Less" in lb for lb in labels)
    assert any("Cheaper" in lb for lb in labels)


@_skip_v1_flow
@pytest.mark.asyncio
async def test_session_caches_last_results_and_shown_ids(store, taste_store, stub_port, adapter):
    """REQ-COMPAT-006 / REQ-COMPAT-007 — shown_product_ids accumulates.

    SPEC-AGENT-V2-REACT (Bucket B2) — V1 search_node session result caching.
    Under V2 result bookkeeping is mediated by the agent's search tool.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "tee"
    store.update(sess)
    stub_port.candidates = [FakeCandidate(id="p1"), FakeCandidate(id="p2")]

    await _run(make_msg(text="something cheaper"))
    sess_after = store.get_or_create(42)
    assert len(sess_after.last_results) == 2
    assert "p1" in sess_after.shown_product_ids
    assert "p2" in sess_after.shown_product_ids
    assert sess_after.state == SessionState.RESULTS_SENT


# ── REQ-COMPAT-002 / REQ-COMPAT-005 — text refine in RESULTS_SENT ──────────


@_skip_v1_flow
@pytest.mark.asyncio
async def test_text_in_results_sent_triggers_critique_path(store, taste_store, stub_port, adapter, monkeypatch):
    """REQ-COMPAT-002 — free-text refine in RESULTS_SENT routes via router.

    SPEC-AGENT-V2-REACT (Bucket B2) — V1 router CRITIQUE_TEXT → critique_apply
    → search with delta. Under V2 free-text refine is the agent's
    refine_search tool decision.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.RESULTS_SENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "white tee"
    sess.vision_keywords = ["tee", "white"]
    store.update(sess)

    from app.channels.critique import CritiqueDelta
    from app.channels.router import RoutedDecision, RoutedIntent

    async def _stub_route(text, state, last_results):
        return RoutedDecision(
            intent=RoutedIntent.CRITIQUE_TEXT,
            critique_delta=CritiqueDelta(op="free_text", color="black", extra_intent=text[:200]),
        )

    monkeypatch.setattr("app.channels.router.settings.ROUTER_LLM_ENABLED", True)
    monkeypatch.setattr("app.graphs.nodes.ingest.route_text", _stub_route)

    await _run(make_msg(text="in black"))
    assert stub_port.calls
    req = stub_port.calls[-1]
    assert req.color == "black"


# ── REQ-MIGR-001 / REQ-MIGR-002 — scenario module deleted ──────────────────


def test_scenario_module_deleted():
    """REQ-MIGR-001 / REQ-MIGR-002 — `app.channels.scenario` is gone."""
    with pytest.raises(ModuleNotFoundError):
        import app.channels.scenario  # noqa: F401


# ── REQ-COMPAT-002 — picker tap → intent reply → search regression ─────────


@_skip_v1_flow
@pytest.mark.asyncio
async def test_pick_callback_then_intent_text_runs_search(store, taste_store, stub_port, adapter):
    """SPEC-AGENT-V2-REACT (Bucket B2) — the bare-pick step is deterministic in
    V2 (pick_item node preserved) but the follow-up intent-text → search step
    is agent-tool-mediated; the test asserts the V1 search-routing tail.

    REQ-COMPAT-002 — after picker tap (item:0), session is AWAITING_INTENT.
    Subsequent text reply must run search and dispatch cards. Regression for
    SPEC-AGENT-001 migration: original `_route_after_pick` routed to
    `critique_apply` which had no handler for `item:N` callbacks → silent
    dead-end. Bare picker tap now routes to respond (OPENER prompt) and the
    next text turn drives the search.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_ITEM_PICK
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.detected_items = [
        {"label": "blue striped sweater", "description": "stripes", "keywords": ["sweater"]},
        {"label": "white tee", "description": "round neck", "keywords": ["tee"]},
    ]
    store.update(sess)

    # Step A: user taps button 1 → callback item:0
    await _run(make_msg(callback_data="item:0"))
    sess_a = store.get_or_create(42)
    assert sess_a.state == SessionState.AWAITING_INTENT, "picker tap must set AWAITING_INTENT"
    assert sess_a.vision_item == "blue striped sweater"
    # Search MUST NOT run on bare picker tap (matches original scenario behavior).
    assert stub_port.calls == [], f"search should not run on bare pick; calls={len(stub_port.calls)}"
    # Bot must have answered the callback or sent a prompt — never silent dead-end.
    assert adapter.callback_answers or adapter.texts, "bot must respond to picker tap"

    # Step B: user types intent reply
    await _run(make_msg(text="20만원 이하로 찾고싶어"))
    assert stub_port.calls, "search MUST run on AWAITING_INTENT + text"
    assert adapter.cards, f"cards must be dispatched, got texts={adapter.texts}"


@_skip_v1_flow
@pytest.mark.asyncio
async def test_full_pinterest_flow_pick_then_intent(store, taste_store, stub_port, adapter, monkeypatch):
    """SPEC-AGENT-V2-REACT (Bucket B2) — the Pinterest→vision→picker prefix is
    deterministic in V2 but the final intent-text → search → cards step is
    agent-tool-mediated; the test asserts the V1 search-routing tail.

    REQ-COMPAT-002 — end-to-end: Pinterest URL → vision multi → picker →
    tap → intent text → search → cards dispatched. Validates that resolve_image
    + vision_node persist `image_url` / `detected_items` to the session so the
    later turns can resolve item context.
    """
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_multi(_url):
        return {
            "items": [
                {
                    "label": "blue striped sweater",
                    "description": "long sleeve crew neck cotton",
                    "keywords": ["sweater", "stripes"],
                },
                {"label": "denim jeans", "description": "slim fit dark wash", "keywords": ["jeans"]},
                {"label": "leather sneakers", "description": "white low top", "keywords": ["sneakers"]},
                {"label": "wool beanie", "description": "knit ribbed", "keywords": ["beanie"]},
            ]
        }

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_multi)

    # Turn 1: Pinterest URL → picker carousel
    await _run(make_msg(urls=["https://pin.it/abc"]))
    assert adapter.buttons, "picker must be sent"
    sess1 = store.get_or_create(42)
    assert sess1.state == SessionState.AWAITING_ITEM_PICK
    assert sess1.image_url == "https://i.pinimg.com/originals/x.jpg", "resolve_image must persist image_url to session"
    assert len(sess1.detected_items) == 4

    # Turn 2: tap button 1 (item:0)
    await _run(make_msg(callback_data="item:0"))
    sess2 = store.get_or_create(42)
    assert sess2.state == SessionState.AWAITING_INTENT
    assert sess2.vision_item == "blue striped sweater"
    assert stub_port.calls == [], "no search yet on bare pick"

    # Turn 3: intent reply triggers search + cards
    await _run(make_msg(text="20만원 이하로 찾고싶어"))
    assert stub_port.calls, "search MUST run after intent reply"
    assert adapter.cards, f"cards must be dispatched; got texts={adapter.texts!r}"


@pytest.mark.asyncio
async def test_session_image_url_persisted_after_resolve_vision(store, taste_store, stub_port, adapter, monkeypatch):
    """REQ-COMPAT-002 — resolve_image + vision_node must persist `image_url`,
    `vision_item`, `vision_keywords`, and `detected_items` to the session so
    later refines/critique callbacks (which read sess.image_url) can search.
    Without this, search_node short-circuits at "no image_url; cannot search".
    """
    import app.graphs.nodes.resolve_image as ri
    import app.graphs.nodes.vision as vn

    async def _resolve_ok(_u):
        return ["https://i.pinimg.com/originals/x.jpg"]

    async def _vision_single(_url):
        return {"items": [{"label": "white tee", "description": "round neck slim fit", "keywords": ["tee"]}]}

    monkeypatch.setattr(ri.link_resolver, "resolve", _resolve_ok)
    monkeypatch.setattr(vn.vision_module, "extract", _vision_single)

    await _run(make_msg(urls=["https://pin.it/abc"]))

    sess = store.get_or_create(42)
    assert sess.image_url == "https://i.pinimg.com/originals/x.jpg", "image_url must be persisted"
    assert sess.vision_item == "white tee", "vision_item must be persisted (single-item path)"
    assert sess.vision_keywords == ["tee"], "vision_keywords must be persisted (single-item path)"
    assert sess.detected_items, "detected_items must be persisted"


# ── REQ-AGENT-008 — one webhook = one graph execution ─────────────────────


@_skip_v1_flow
@pytest.mark.asyncio
async def test_two_consecutive_webhooks_run_two_graph_executions(store, taste_store, stub_port, adapter):
    """REQ-AGENT-008 — each ainvoke is a distinct execution.

    Verified by the StubPort.calls counter — two valid search-triggering
    inputs result in two port calls.

    SPEC-AGENT-V2-REACT (Bucket B2) — counts V1 search-node invocations
    (stub_port.calls == 2). Under V2 the search-tool dispatch is
    agent-mediated; this is a pure V1 topology detail.
    """
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    sess.image_url = "https://i.pinimg.com/originals/x.jpg"
    sess.vision_item = "tee"
    store.update(sess)

    await _run(make_msg(text="cheaper"))
    # Reset session state for second turn
    sess = store.get_or_create(42)
    sess.state = SessionState.AWAITING_INTENT
    store.update(sess)
    await _run(make_msg(text="darker"))

    assert len(stub_port.calls) == 2
