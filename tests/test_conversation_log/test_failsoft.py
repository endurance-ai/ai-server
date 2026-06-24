"""SPEC-CONVERSATION-LOG-001 / REQ-LOG-FAILSOFT-001 — log_event NEVER raises.

Scenarios:
- backend flag = "stderr" → emit() writes JSON line, no exception.
- emit() with unserializable nested object → degrades gracefully.
- The graph state surface is *never* mutated by emit failure (we pass an
  immutable mapping in and verify it's unchanged).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.observability.conversation_log as conv_log_mod
from app.observability.conversation_log import emit, log_event


@pytest.mark.asyncio
async def test_log_event_routes_to_stderr_when_backend_is_stderr(monkeypatch, capsys):
    """When `app.state.conv_log_backend == 'stderr'`, log_event writes a
    CONV_LOG_FALLBACK record to stderr instead of attempting DB insert."""
    # Stub the app object — backend_is_postgres reads from app.main.app.state.
    fake_app = SimpleNamespace(state=SimpleNamespace(conv_log_backend="stderr"))
    monkeypatch.setattr("app.main.app", fake_app, raising=False)

    await log_event(
        event_type="user_text",
        user_key="u:7",
        chat_id=7,
        thread_id=uuid4(),
        turn_no=0,
        payload={"text": "hello"},
    )
    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["tag"] == "CONV_LOG_FALLBACK"
    assert record["event_type"] == "user_text"
    assert record["error_phase"] == "backend_unavailable"


@pytest.mark.asyncio
async def test_log_event_swallows_pool_get_failure(monkeypatch, capsys):
    """When backend is postgres but pool acquire raises → stderr fallback, no propagate."""
    fake_app = SimpleNamespace(state=SimpleNamespace(conv_log_backend="postgres"))
    monkeypatch.setattr("app.main.app", fake_app, raising=False)

    def _bad_pool():
        raise RuntimeError("pool not initialized")

    monkeypatch.setattr("app.providers.db_pool.get_pool", _bad_pool)

    # MUST NOT raise.
    await log_event(
        event_type="search_done",
        user_key="u:7",
        chat_id=7,
        thread_id=uuid4(),
        turn_no=6,
        payload={"top_k_product_ids": ["p1"], "rrf_scores": [0.9]},
    )
    captured = capsys.readouterr()
    line = captured.err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["error_phase"] == "pool_acquire"
    assert record["error_type"] == "RuntimeError"


def test_emit_input_payload_not_mutated():
    """Original caller payload object is not modified by truncation."""
    payload = {"text": "x" * 5000, "items": list(range(100))}
    snapshot = {"text": payload["text"], "items": list(payload["items"])}
    emit(
        event_type="user_text",
        user_key="u:1",
        chat_id=1,
        thread_id=uuid4(),
        turn_no=0,
        payload=payload,
    )
    assert payload == snapshot, "emit() must not mutate caller payload"


def test_stderr_fallback_record_is_single_line_json(capsys, monkeypatch):
    # pool_loop 이 있으면 run_coroutine_threadsafe 경로로 처리되어 stderr 출력이 없다.
    # pool 미초기화 상태를 강제해 no_event_loop 분기(sync 호출 fallback)를 검증한다.
    import app.providers.db_pool as _db_pool

    monkeypatch.setattr(_db_pool, "_loop", None)
    emit(
        event_type="node_error",
        user_key="u:9",
        chat_id=9,
        thread_id=uuid4(),
        turn_no=3,
        payload={"node_name": "vision", "exception_type": "ValueError", "message": "boom", "recovered": False},
    )
    captured = capsys.readouterr()
    err = captured.err.strip()
    assert err, "expected stderr output"
    # Each emit writes exactly one line (no embedded newlines in JSON).
    lines = err.splitlines()
    last = lines[-1]
    record = json.loads(last)
    assert record["tag"] == "CONV_LOG_FALLBACK"
    # security review P1-02: stderr fallback strips payload body to prevent
    # PII / GDPR cascade. Only metadata + payload_keys count are recorded.
    assert "payload" not in record, "payload body must NOT be in stderr fallback (security P1-02)"
    assert record["event_type"] == "node_error"
    assert record["user_key"] == "u:9"


def test_concurrent_emits_no_exception():
    """Stress: many rapid emits in sync context — all degrade gracefully."""
    for i in range(50):
        emit(
            event_type="user_text",
            user_key=f"u:{i}",
            chat_id=i,
            thread_id=uuid4(),
            turn_no=0,
            payload={"text": f"msg-{i}"},
        )


def test_log_event_module_level_singleton_strong_set():
    """The retention set is a strong-ref set at module level.

    code review P0-1 fix: WeakSet allowed GC race where caller-discarded tasks
    were collected before INSERT started. Strong set + add_done_callback(set.discard)
    keeps tasks alive until completion, then evicts.
    """
    assert isinstance(conv_log_mod._IN_FLIGHT, set)
    # Not a WeakSet (regression guard).
    from weakref import WeakSet

    assert not isinstance(conv_log_mod._IN_FLIGHT, WeakSet)
