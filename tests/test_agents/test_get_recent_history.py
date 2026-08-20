"""Unit tests for the get_recent_history tool wrapper.

SPEC-AGENT-V2-REACT / T-003f. Covers:
- the no-user_key and non-postgres-backend fail-soft empty paths,
- the postgres SELECT path with a mocked pool (row → summarized event),
- the per-event-type `_summarize_payload` whitelist branches.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from app.agents.tools import get_recent_history as tool

# asyncio_mode=auto handles async tests; no module-level asyncio mark (it would
# wrongly tag the sync _summarize_payload tests below).


# ── _summarize_payload whitelist (pure) ─────────────────────────────────────


def test_summarize_user_text_truncates():
    out = tool._summarize_payload("user_text", {"text": "x" * 1000})
    assert len(out["text"]) == 500


def test_summarize_user_photo_hashes_url():
    out = tool._summarize_payload("user_photo", {"image_url": "http://x/y.jpg"})
    assert out["image_url_hash"] is not None
    assert len(out["image_url_hash"]) == 16


def test_summarize_user_photo_no_url_is_none():
    assert tool._summarize_payload("user_photo", {})["image_url_hash"] is None


def test_summarize_user_callback():
    assert tool._summarize_payload("user_callback", {"callback_data": "cb:1"}) == {"callback_data": "cb:1"}


def test_summarize_intent_routed():
    out = tool._summarize_payload("intent_routed", {"intent": "search", "critique_delta_summary": "s"})
    assert out == {"intent": "search", "critique_delta_summary": "s"}


def test_summarize_vision_done_caps_mood_and_counts_items():
    out = tool._summarize_payload(
        "vision_done",
        {"style_node_primary": "A", "mood": ["a", "b", "c", "d"], "items": [1, 2]},
    )
    assert out["style_node_primary"] == "A"
    assert out["mood"] == ["a", "b", "c"]
    assert out["items_count"] == 2


def test_summarize_search_done_extracts_filters():
    out = tool._summarize_payload(
        "search_done",
        {
            "query": {"text_query": "grey dress"},
            "filters": {"category": "dress", "subcategory": "midi", "brand_names": ["ami", "acne"]},
            "is_refine": True,
            "top_k_product_ids": ["p1", "p2"],
            "dense_count": 50,
        },
    )
    assert out["text_query"] == "grey dress"
    assert out["category"] == "dress"
    assert out["subcategory"] == "midi"
    assert out["brand_names"] == ["ami", "acne"]
    assert out["is_refine"] is True
    assert out["top_k_product_ids"] == ["p1", "p2"]
    assert out["raw_count"] == 50


def test_summarize_search_done_tolerates_non_dict_filters():
    out = tool._summarize_payload("search_done", {"query": {}, "filters": "garbage"})
    assert out["category"] is None
    assert out["brand_names"] == []


def test_summarize_diversify_done():
    assert tool._summarize_payload("diversify_done", {"output_count": 12}) == {"final_count": 12}


def test_summarize_card_sent_and_clicked():
    assert tool._summarize_payload("card_sent", {"product_id": "p", "position": 1}) == {
        "product_id": "p",
        "position": 1,
    }
    assert tool._summarize_payload("card_clicked", {"product_id": "p", "position": 2}) == {
        "product_id": "p",
        "position": 2,
    }


def test_summarize_bot_text_truncates():
    out = tool._summarize_payload("bot_text", {"chunk_text": "y" * 1000})
    assert len(out["text"]) == 500


def test_summarize_taste_update():
    assert tool._summarize_payload("taste_update", {}) == {"applied": True}


def test_summarize_tool_call():
    out = tool._summarize_payload(
        "tool_call",
        {"tool_name": "search_products", "iteration_no": 1, "latency_ms": 12, "error": None, "args_summary": "s"},
    )
    assert out["tool_name"] == "search_products"
    assert out["args_summary"] == "s"


def test_summarize_ask_clarify_sent_caps_options():
    out = tool._summarize_payload("ask_clarify_sent", {"axis": "fit", "options": list(range(10))})
    assert out["axis"] == "fit"
    assert len(out["options"]) == 6


def test_summarize_node_error():
    out = tool._summarize_payload(
        "node_error", {"node_name": "agent", "exception_type": "ValueError", "recovered": True}
    )
    assert out == {"node_name": "agent", "exception_type": "ValueError", "recovered": True}


def test_summarize_unknown_event_type_passthrough():
    assert tool._summarize_payload("mystery", {}) == {"event_type": "mystery"}


# ── dispatch fail-soft paths ────────────────────────────────────────────────


async def test_dispatch_no_user_key_returns_empty():
    res = await tool.dispatch({"n": 5}, {})
    assert res == {"ok": True, "error": None, "events": []}


async def test_dispatch_non_postgres_backend_returns_empty(monkeypatch):
    from app.main import app

    monkeypatch.setattr(app.state, "conv_log_backend", "memory", raising=False)
    res = await tool.dispatch({"n": 5}, {"user_key": "u:1"})
    assert res == {"ok": True, "error": None, "events": []}


# ── dispatch postgres SELECT path (mocked pool) ─────────────────────────────


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    async def execute(self, sql, params):
        self.executed = (sql, params)

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor_obj):
        self._cursor_obj = cursor_obj

    @asynccontextmanager
    async def _cursor_cm(self):
        yield self._cursor_obj

    def cursor(self):
        return self._cursor_cm()


class _FakePool:
    def __init__(self, rows):
        self._rows = rows
        self.cursor_obj = _FakeCursor(rows)

    @asynccontextmanager
    async def connection(self):
        yield _FakeConn(self.cursor_obj)


async def test_dispatch_postgres_returns_summarized_events(monkeypatch):
    from app.main import app
    from app.providers import db_pool

    monkeypatch.setattr(app.state, "conv_log_backend", "postgres", raising=False)

    rows = [
        ("user_text", {"text": "hello"}, datetime(2026, 6, 1)),
        ("search_done", {"query": {"text_query": "q"}, "filters": {}}, datetime(2026, 6, 1)),
    ]
    fake_pool = _FakePool(rows)
    monkeypatch.setattr(db_pool, "get_pool", lambda: fake_pool)

    thread_id = uuid4()
    res = await tool.dispatch(
        {"n": 5, "event_types": ["user_text", "search_done"]}, {"user_key": "u:1", "thread_id": thread_id}
    )
    assert res["ok"] is True
    assert [e["event_type"] for e in res["events"]] == ["user_text", "search_done"]
    assert res["events"][0]["payload_summary"]["text"] == "hello"
    # event_types filter appended ANY clause + user/thread scope + limit params.
    sql, params = fake_pool.cursor_obj.executed
    assert "ANY(%s)" in sql
    assert "thread_id=%s" in sql
    assert params == ("u:1", thread_id, ["user_text", "search_done"], 5)


async def test_dispatch_postgres_db_error_returns_empty(monkeypatch):
    from app.main import app
    from app.providers import db_pool

    monkeypatch.setattr(app.state, "conv_log_backend", "postgres", raising=False)

    def _boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr(db_pool, "get_pool", _boom)
    res = await tool.dispatch({"n": 5}, {"user_key": "u:1", "thread_id": uuid4()})
    assert res == {"ok": True, "error": None, "events": []}


async def test_dispatch_clamps_n_to_bounds(monkeypatch):
    from app.main import app
    from app.providers import db_pool

    monkeypatch.setattr(app.state, "conv_log_backend", "postgres", raising=False)
    fake_pool = _FakePool([])
    monkeypatch.setattr(db_pool, "get_pool", lambda: fake_pool)

    await tool.dispatch({"n": 999}, {"user_key": "u:1", "thread_id": uuid4()})
    _, params = fake_pool.cursor_obj.executed
    assert params[-1] == 20  # clamped to max 20
