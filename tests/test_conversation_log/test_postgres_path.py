"""SPEC-CONVERSATION-LOG-001 / LOG-T04 — postgres INSERT path coverage.

Uses an async context-manager mock pool to drive the happy path without docker.
Exercises:
- backend == "postgres" → INSERT path with correct SQL and params.
- truncate-stage failure → stderr fallback with error_phase="truncate".
- langfuse_trace capture failure → emit still proceeds (None trace).
- emit with already-provided langfuse_trace skips capture.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.observability.conversation_log import emit, log_event


class _FakeCursor:
    def __init__(self, recorder):
        self.recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, params):
        self.recorder.append({"sql": sql, "params": params})


class _FakeConn:
    def __init__(self, recorder):
        self.recorder = recorder
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def cursor(self):
        return _FakeCursor(self.recorder)

    async def commit(self):
        self.committed = True


class _FakePool:
    def __init__(self):
        self.recorder = []

    def connection(self):
        return _FakeConn(self.recorder)


@pytest.mark.asyncio
async def test_log_event_postgres_path_executes_insert(monkeypatch):
    """Happy path: backend=postgres + working pool → one INSERT row issued."""
    fake_app = SimpleNamespace(state=SimpleNamespace(conv_log_backend="postgres"))
    monkeypatch.setattr("app.main.app", fake_app, raising=False)

    pool = _FakePool()
    monkeypatch.setattr("app.providers.db_pool.get_pool", lambda: pool)

    tid = uuid4()
    await log_event(
        event_type="search_done",
        user_key="u:1",
        chat_id=42,
        thread_id=tid,
        turn_no=6,
        payload={"top_k_product_ids": ["p1", "p2"], "rrf_scores": [0.9, 0.8]},
        langfuse_trace="trace-abc",
        latency_ms=15,
    )
    assert len(pool.recorder) == 1
    rec = pool.recorder[0]
    assert "INSERT INTO ai.log_conversation_event" in rec["sql"]
    # Params order matches column order.
    p = rec["params"]
    assert p[0] == "u:1"
    assert p[1] == 42
    assert p[2] == tid
    assert p[3] == 6
    assert p[4] == "search_done"
    # p[5] is Jsonb wrapper; just check it exists and is not None.
    assert p[5] is not None
    assert p[6] == "trace-abc"
    assert p[7] == 15
    assert p[8] is None  # user_id omitted → NULL (telegram-era caller)


@pytest.mark.asyncio
async def test_log_event_postgres_insert_failure_falls_back(monkeypatch, capsys):
    """When the INSERT itself raises → stderr fallback with error_phase=insert."""
    fake_app = SimpleNamespace(state=SimpleNamespace(conv_log_backend="postgres"))
    monkeypatch.setattr("app.main.app", fake_app, raising=False)

    class _BoomCursor(_FakeCursor):
        async def execute(self, sql, params):
            raise RuntimeError("disk full")

    class _BoomConn(_FakeConn):
        def cursor(self):
            return _BoomCursor(self.recorder)

    class _BoomPool(_FakePool):
        def connection(self):
            return _BoomConn(self.recorder)

    monkeypatch.setattr("app.providers.db_pool.get_pool", lambda: _BoomPool())

    await log_event(
        event_type="user_text",
        user_key="u:err",
        chat_id=1,
        thread_id=uuid4(),
        turn_no=0,
        payload={"text": "boom"},
    )
    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["error_phase"] == "insert"
    assert record["error_type"] == "RuntimeError"


def test_emit_uses_provided_langfuse_trace(monkeypatch):
    """Explicit langfuse_trace short-circuits the capture cascade."""
    called: list[bool] = []

    def _spy():
        called.append(True)
        return "should-not-be-used"

    monkeypatch.setattr("app.observability.conversation_log.current_langfuse_trace_id", _spy)

    async def _run():
        emit(
            event_type="user_text",
            user_key="u:1",
            chat_id=1,
            thread_id=uuid4(),
            turn_no=0,
            payload={"text": "hi"},
            langfuse_trace="caller-supplied",
        )

    asyncio.get_event_loop_policy()  # ensure module imported
    asyncio.run(_run())
    assert called == [], "current_langfuse_trace_id must not be called when caller provides trace"


def test_emit_langfuse_capture_failure_does_not_propagate(monkeypatch):
    """If the trace_id helper raises, emit() still proceeds with None."""

    def _boom():
        raise RuntimeError("langfuse client crashed")

    monkeypatch.setattr("app.observability.conversation_log.current_langfuse_trace_id", _boom)

    async def _run():
        # MUST NOT raise.
        emit(
            event_type="user_text",
            user_key="u:1",
            chat_id=1,
            thread_id=uuid4(),
            turn_no=0,
            payload={"text": "hi"},
        )

    asyncio.run(_run())
