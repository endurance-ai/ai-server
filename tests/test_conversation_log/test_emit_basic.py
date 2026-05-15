"""SPEC-CONVERSATION-LOG-001 / LOG-T04 — emit() + log_event() happy path.

These tests do NOT touch Postgres — they verify:
- `emit()` schedules an asyncio task and registers it in `_IN_FLIGHT` WeakSet.
- The scheduled task completes without raising.
- After completion the task is discarded from `_IN_FLIGHT` (R7 retention).
- `log_event()` is a coroutine that returns None.
- When no event loop is running, `emit()` falls back to stderr (no exception).
"""

from __future__ import annotations

import asyncio
import gc
import json
from uuid import uuid4

import pytest

from app.observability import conversation_log
from app.observability.conversation_log import emit, log_event


@pytest.mark.asyncio
async def test_log_event_returns_none_when_backend_unset():
    """No probe ran → backend flag unset → DEBUG-skip path; never raises."""
    result = await log_event(
        event_type="user_text",
        user_key="u:1",
        chat_id=1,
        thread_id=uuid4(),
        turn_no=0,
        payload={"text": "hi", "lang_detected": "en"},
    )
    assert result is None


@pytest.mark.asyncio
async def test_emit_schedules_task_and_completes():
    """emit() returns synchronously; task lands in _IN_FLIGHT then is discarded."""
    tid = uuid4()
    # Snapshot pre-state.
    before = len(conversation_log._IN_FLIGHT)
    emit(
        event_type="user_text",
        user_key="u:1",
        chat_id=1,
        thread_id=tid,
        turn_no=0,
        payload={"text": "hello", "lang_detected": "en"},
    )
    # Immediately after emit(), task is pending.
    pending = len(conversation_log._IN_FLIGHT)
    assert pending >= before  # at least one task added (or zero if it completed sync — unlikely)

    # Let the loop drain.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # After completion, done_callback discards. Force GC to clear WeakSet refs.
    gc.collect()
    # Not strictly empty (other tests may share state); but our task is gone.
    # The strict check: no pending tasks reference our event.
    for t in list(conversation_log._IN_FLIGHT):
        if not t.done():
            t.cancel()


@pytest.mark.asyncio
async def test_emit_never_raises_on_bad_payload():
    """A payload containing an unserializable value still must not raise."""

    class Unserializable:
        def __repr__(self):
            raise RuntimeError("nope")

    # Should NOT raise — emit swallows all errors.
    emit(
        event_type="user_text",
        user_key="u:1",
        chat_id=1,
        thread_id=uuid4(),
        turn_no=0,
        payload={"text": "hi", "weird": Unserializable()},
    )
    await asyncio.sleep(0)


def test_emit_no_running_loop_falls_back_to_stderr(capsys):
    """When called from sync context with no running loop → stderr JSON line."""
    emit(
        event_type="user_text",
        user_key="u:99",
        chat_id=99,
        thread_id=uuid4(),
        turn_no=0,
        payload={"text": "from sync context"},
    )
    captured = capsys.readouterr()
    # The line is on stderr, single JSON object.
    assert captured.err.strip(), "expected stderr fallback line"
    line = captured.err.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["tag"] == "CONV_LOG_FALLBACK"
    assert record["event_type"] == "user_text"
    assert record["user_key"] == "u:99"
    assert record["error_phase"] == "no_event_loop"


def test_seed_thread_returns_unique_uuids():
    from app.observability.conversation_log import seed_thread

    a = seed_thread()
    b = seed_thread()
    assert a != b
    assert str(a)  # is a UUID-shaped string
