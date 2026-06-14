"""fix/langfuse-trace-io — recommendation-turn trace I/O.

The Langfuse LLM-as-judge (`recommendation_quality`) reads a trace's
top-level `input`/`output`. The app only created `@observe` spans, so a real
`webhook.telegram` trace had `input=null`/`output=null` and the judge had
nothing to score.

These tests assert the `respond` tool — the ReAct recommendation
finalize+delivery seam — sets `update_current_trace(input=..., output=...)`
with the user's request semantics + the delivered product set, that no raw
identifier leaks (STRUCTURAL check, not a substring scan), that a failure to
do so is swallowed (delivery still succeeds), and that an interrupted-then-
retried turn sets trace I/O exactly once (not null, not double).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.infrastructure.memory.session import (
    Session,
    SessionState,
    get_store,
    set_store,
)


class _FakeStore:
    def __init__(self, sess: Session) -> None:
        self._sess = sess

    def get_or_create(self, chat_id: int) -> Session:
        return self._sess

    def update(self, sess: Session) -> None:  # noqa: D401 — protocol parity
        self._sess = sess


def _candidate(i: int):
    from app.models.response import Candidate

    return Candidate(
        id=f"p{i}",
        brand=f"Brand{i}",
        name=f"Slim Tee {i}",
        price=39000 + i,
        image_url=f"https://img.example.com/{i}.jpg",
        product_url=f"https://shop.example.com/{i}",
        platform="kiko",
        subcategory="t-shirt",
        score=0.9 - i * 0.01,
    )


def _session_with_results(n: int) -> Session:
    s = Session(chat_id=42, state=SessionState.IDLE)
    s.last_results = [_candidate(i) for i in range(n)]
    return s


def _walk(obj: Any):
    """Yield ('__key__', k) for every dict key and ('__leaf__', v) for every
    scalar leaf in a nested dict/list structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield ("__key__", k)
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)
    else:
        yield ("__leaf__", obj)


def _assert_no_pii(payload: Any, *, forbidden_keys: set[str], forbidden_values: set[Any]) -> None:
    """STRUCTURAL leak check: no dict ANYWHERE in `payload` may carry a key in
    `forbidden_keys`, and no scalar leaf may equal a forbidden identifier
    value (compared per-leaf, not via a str(dict) substring scan which
    false-negatives when '42' appears inside a product id/price)."""
    forbidden_str = {str(v) for v in forbidden_values}
    for kind, item in _walk(payload):
        if kind == "__key__":
            assert item not in forbidden_keys, f"PII key leaked: {item!r}"
        else:
            assert item not in forbidden_values, f"PII value leaked: {item!r}"
            assert str(item) not in forbidden_str, f"PII value leaked: {item!r}"


@pytest.fixture
def _restore_store():
    """Save/restore the active store so a test never leaks its fake (or None)
    into later tests."""
    _orig = get_store()
    try:
        yield
    finally:
        set_store(_orig)


@pytest.fixture(autouse=True)
def _mock_chat_state_redis(monkeypatch):
    """Isolate tests from real Redis state (cross-turn impression dedup).

    The 260611 UX dedup feature gates send_hybrid_batch on chat_state.is_logged.
    Without this fixture, products left in Redis from prior test runs are treated
    as "already shown" and filtered out, causing cards_sent==0 instead of N.
    """
    monkeypatch.setattr(
        "app.infrastructure.cache.chat_state.is_logged",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.infrastructure.cache.chat_state.mark_logged",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.infrastructure.cache.chat_state.get_cursor",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.infrastructure.cache.chat_state.set_cursor",
        AsyncMock(),
    )


@pytest.mark.asyncio
async def test_recommendation_turn_sets_trace_input_output(monkeypatch, _restore_store):
    """On the delivered recommendation path, the root trace gets a
    judge-readable `input` (user query + vision attrs) and `output`
    (recommendation result set {product_id, brand, title} + reply)."""
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(3)))
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.observability.langfuse.update_current_trace",
        lambda metadata=None, **kw: captured.update(kw),
    )

    adapter = MagicMock()
    adapter.send_text = AsyncMock()
    adapter.send_card = AsyncMock(return_value=1001)
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

    ctx = {
        "chat_id": 42,
        "from_user_id": 777,  # MUST NOT leak into trace I/O
        "text_query": "slim black tee",
        "vision_category": "Top",
        "style_node_primary": "A",
        "lang": "en",
        CARDS_READY_KEY: True,
    }
    res = await respond_tool.dispatch({"text": "Here are some matches!"}, ctx)

    assert res["ok"] is True
    assert res["cards_sent"] == 3

    ti = captured["input"]
    assert ti["query"] == "slim black tee"
    assert ti["vision"] == {"category": "Top", "style_node": "A"}
    assert ti["lang"] == "en"

    to = captured["output"]
    assert to["count"] == 3
    assert {p["product_id"] for p in to["products"]} == {"p0", "p1", "p2"}
    assert to["products"][0]["brand"] == "Brand0"
    assert to["products"][0]["title"] == "Slim Tee 0"
    assert to["reply"] == "Here are some matches!"

    # STRUCTURAL PII check: no chat_id/from_user_id keys anywhere, and the
    # identifier *values* 42 / 777 do not appear as scalar leaves.
    _assert_no_pii(
        {"input": ti, "output": to},
        forbidden_keys={"chat_id", "from_user_id"},
        forbidden_values={42, 777},
    )


@pytest.mark.asyncio
async def test_trace_io_failure_is_swallowed(monkeypatch, _restore_store):
    """A raising `update_current_trace` must NOT break or delay delivery —
    the recommendation result is already sent (fail-open invariant)."""
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.search_products import CARDS_READY_KEY

    set_store(_FakeStore(_session_with_results(2)))

    def _boom(metadata=None, **kwargs):  # noqa: ANN001
        raise RuntimeError("langfuse exploded")

    monkeypatch.setattr("app.observability.langfuse.update_current_trace", _boom)

    adapter = MagicMock()
    adapter.send_text = AsyncMock()
    adapter.send_card = AsyncMock(return_value=1001)
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

    ctx = {"chat_id": 42, "text_query": "loafers", "lang": "en", CARDS_READY_KEY: True}
    res = await respond_tool.dispatch({"text": "found these!"}, ctx)

    assert res["ok"] is True
    assert res["cards_sent"] == 2
    adapter.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_only_turn_sets_input_and_empty_product_output(monkeypatch, _restore_store):
    """A no-card turn still gives the judge the request `input` and an empty
    product `output` (no stale product set, reply preserved)."""
    from app.agents.tools import respond as respond_tool

    set_store(_FakeStore(Session(chat_id=42, state=SessionState.IDLE)))
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "app.observability.langfuse.update_current_trace",
        lambda metadata=None, **kw: captured.update(kw),
    )

    adapter = MagicMock()
    adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

    ctx = {"chat_id": 42, "text_query": "hello", "lang": "en"}
    res = await respond_tool.dispatch({"text": "hi there!"}, ctx)

    assert res["ok"] is True
    assert res["cards_sent"] == 0
    assert captured["input"]["query"] == "hello"
    assert captured["output"]["products"] == []
    assert captured["output"]["count"] == 0
    assert captured["output"]["reply"] == "hi there!"


@pytest.mark.asyncio
async def test_partial_delivery_reentry_sets_trace_io_exactly_once(monkeypatch, _restore_store):
    """Interrupted-then-retried turn: a prior entry sent the text but the
    carousel was interrupted before the genuine-completion branch. The retry
    enters with `_TEXT_SENT_KEY` set and nothing to send this entry, hits the
    partial-delivery terminal branch, and MUST set trace I/O exactly once
    (not null because an entry delivered something; not double-set)."""
    from app.agents.tools import respond as respond_tool
    from app.agents.tools.respond import _TEXT_SENT_KEY

    set_store(_FakeStore(_session_with_results(2)))
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        "app.observability.langfuse.update_current_trace",
        lambda metadata=None, **kw: calls.append(dict(kw)),
    )

    adapter = MagicMock()
    adapter.send_text = AsyncMock()
    monkeypatch.setattr("app.graphs.nodes._adapter_ctx.get_adapter", lambda: adapter)

    # Simulate a prior interrupted entry: text was already sent, but the turn
    # never reached genuine completion (_DONE_KEY / _TRACE_IO_SET_KEY unset).
    ctx: dict[str, object] = {
        "chat_id": 42,
        "text_query": "jackets",
        "lang": "en",
        _TEXT_SENT_KEY: True,
    }

    # Retry entry: no CARDS_READY_KEY and text already sent → nothing sent THIS
    # entry → partial-delivery terminal branch.
    res = await respond_tool.dispatch({"text": "here you go!"}, ctx)

    assert res["ok"] is True
    assert res["text_sent"] is False  # not resent this entry
    # Trace I/O set exactly once, with non-null input/output.
    assert len(calls) == 1
    assert calls[0]["input"]["query"] == "jackets"
    assert calls[0]["output"]["reply"] == "here you go!"

    # A further defensive re-entry must NOT double-set (idempotent per turn).
    res2 = await respond_tool.dispatch({"text": "here you go!"}, ctx)
    assert res2["ok"] is True
    assert len(calls) == 1
