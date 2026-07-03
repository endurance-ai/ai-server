"""SPEC-CONVERSATION-LOG-001 — conversation event logger (fire-and-forget).

이 모듈은 graph node / webhook intake / implicit-feedback emit site 에서 호출되는
**single entry point** 다. 외부 인터페이스:

- `log_event(...)` (async)   — REQ-LOG-FAILSOFT-001 (never raises).
- `emit(...)` (sync wrapper) — REQ-LOG-FIRE-AND-FORGET-001 (`asyncio.create_task` +
  WeakSet retention; never raises).
- `current_langfuse_trace_id()` — re-exported convenience for callers that already
  imported this module (실제 구현은 `app/observability/langfuse.py`).
- `seed_thread()` — `uuid.uuid4` alias for caller-side clarity.

내부 책임:

- `_truncate(payload)` — REQ-LOG-PAYLOAD-CAP-001 (2048 chars / 50 items / 100 keys).
- `_stderr_fallback(...)` — REQ-LOG-FAILSOFT-001 / OQ-5 single-line JSON tag.
- Backend 선택 — `app.state.conv_log_backend` flag (LOG-T07 lifespan probe 결과)
  가 `"postgres"` 일 때만 INSERT; `"stderr"` (또는 미세팅) 일 때 stderr fallback.

이 모듈은 *graph 노드 본문 무변경* 원칙 (plan §R12) 하에 동작한다 — 호출자는 단지
`emit(...)` 한 줄만 추가하면 된다.
"""

# @MX:ANCHOR: [AUTO] SPEC-CONVERSATION-LOG-001 fire-and-forget logger entry point
# @MX:REASON: 19 event types × 12 graph nodes + webhook intake all funnel through emit()
# @MX:SPEC: SPEC-CONVERSATION-LOG-001

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.channels._jsonable import to_jsonable
from app.observability.langfuse import add_trace_event, current_langfuse_trace_id

logger = logging.getLogger(__name__)

__all__ = [
    "current_langfuse_trace_id",
    "emit",
    "log_event",
    "seed_thread",
]

# ── Payload truncation caps (REQ-LOG-PAYLOAD-CAP-001) ──────────────────────
TEXT_CAP = 2048  # chars
LIST_CAP = 50  # items
DICT_CAP = 100  # keys

# ── In-flight task retention (R7 — prevent GC of pending log tasks) ────────
# @MX:ANCHOR: 강한 참조(set)로 task / future 를 보유해야 함. WeakSet 으로 두면
# caller 가 핸들을 버리는 순간 GC 가 PG INSERT 시작 전에 수거할 수 있어 데이터
# 손실이 발생한다 (code review P0-1).
# @MX:REASON: WeakSet GC race — task lifetime must outlive the eventloop tick.
# @MX:SPEC: SPEC-CONVERSATION-LOG-001
# add_done_callback(_IN_FLIGHT.discard) 가 완료된 task/future 를 즉시 정리하므로
# 메모리 누수 위험은 없다. pool_loop 경로는 concurrent.futures.Future,
# fallback 경로는 asyncio.Task 를 보유 — 둘 다 done()/cancel() 인터페이스 동일.
_IN_FLIGHT: set[Any] = set()


def seed_thread() -> UUID:
    """Return a fresh thread_id (REQ-LOG-THREAD-001)."""
    return uuid4()


# ── Truncation helpers ─────────────────────────────────────────────────────


def _truncate_value(value: Any, *, dict_drop_lowest: bool = False) -> Any:
    """Single value truncation. Recursive descent matches `to_jsonable` shape."""
    if isinstance(value, str):
        return value[:TEXT_CAP]
    if isinstance(value, list):
        capped = value[:LIST_CAP]
        return [_truncate_value(v, dict_drop_lowest=dict_drop_lowest) for v in capped]
    if isinstance(value, dict):
        return _truncate_dict(value, dict_drop_lowest=dict_drop_lowest)
    return value


def _truncate_dict(d: Mapping[str, Any], *, dict_drop_lowest: bool) -> dict[str, Any]:
    """Cap dict to DICT_CAP keys.

    Drop policy:
    - If ALL values are numeric (int/float) AND caller hints `dict_drop_lowest`:
      sort by value desc, keep top-N (taste_update.keywords_delta semantics).
    - Otherwise: sort by key (deterministic), keep first N.
    """
    if len(d) <= DICT_CAP:
        # Still need to recurse into nested structures so deep values truncate.
        return {k: _truncate_value(v, dict_drop_lowest=dict_drop_lowest) for k, v in d.items()}
    items = list(d.items())
    if dict_drop_lowest and all(isinstance(v, (int, float)) and not isinstance(v, bool) for _, v in items):
        items.sort(key=lambda kv: kv[1], reverse=True)
    else:
        items.sort(key=lambda kv: kv[0])
    capped = items[:DICT_CAP]
    return {k: _truncate_value(v, dict_drop_lowest=dict_drop_lowest) for k, v in capped}


def _truncate(payload: Mapping[str, Any], *, event_type: str | None = None) -> dict[str, Any]:
    """Top-level truncation applied before INSERT or stderr fallback.

    REQ-LOG-PAYLOAD-CAP-001 — silent (no node_error). Drop policy for
    `taste_update.keywords_delta` / `taste_update.brands_delta` 는 lowest-weight-first
    이므로 `event_type=='taste_update'` 일 때 그 하위 dict 만 `dict_drop_lowest=True`
    로 전파한다.
    """
    out: dict[str, Any] = {}
    is_taste = event_type == "taste_update"
    for k, v in payload.items():
        if is_taste and k in ("keywords_delta", "brands_delta") and isinstance(v, dict):
            # 1-level nested: each sub-key (liked_added/disliked_added/etc.) may be
            # a list or a weighted dict. The drop-by-weight policy targets the
            # *innermost* weighted dict — apply hint recursively.
            out[k] = _truncate_value(v, dict_drop_lowest=True)
        else:
            out[k] = _truncate_value(v, dict_drop_lowest=False)
    return out


# ── Secret-scrubbing for node_error messages (security review P1-01) ────────
# @MX:ANCHOR: 모든 node_error.payload.message 는 이 함수를 거쳐야 한다.
# @MX:REASON: psycopg DSN / LiteLLM API key / 내부 hostname 이 raw exception
# 메시지에 섞여 영구 저장될 위험 (security review P1-01).
# @MX:SPEC: SPEC-CONVERSATION-LOG-001
_SCRUB_PATTERNS: tuple[tuple[str, str], ...] = (
    # postgresql://user:pass@host/db → scheme + redacted credentials
    (r"postgres(?:ql)?://[^:\s]+:[^@\s]+@[^/\s]+", "postgresql://***:***@***"),
    # Bearer / API key headers (Apify, OpenAI, etc.)
    (r"Bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer ***"),
    (r"sk-[A-Za-z0-9._\-]{16,}", "sk-***"),
    (r"apify_api_[A-Za-z0-9]{16,}", "apify_api_***"),
    # query-string token=... (legacy paths, even though we removed it from Apify)
    (r"(token|api_key|apikey|password|secret|authorization)\s*[=:]\s*[^\s&\"']+", r"\1=***"),
)
_SCRUB_RE_CACHE: list[tuple[re.Pattern[str], str]] = []


def scrub_exception_message(text: str, *, max_chars: int = 500) -> str:
    """Strip secrets from an exception message before persisting to a log row.

    Truncates to `max_chars` and applies a deny-list regex sweep. Always safe
    to call — never raises.
    """
    if not _SCRUB_RE_CACHE:
        for pat, repl in _SCRUB_PATTERNS:
            try:
                _SCRUB_RE_CACHE.append((re.compile(pat, re.IGNORECASE), repl))
            except re.error:
                pass
    try:
        s = str(text)[:max_chars]
        for pat, repl in _SCRUB_RE_CACHE:
            s = pat.sub(repl, s)
        return s
    except Exception:  # noqa: BLE001
        return "<scrub-failed>"


# ── Stderr fallback (REQ-LOG-FAILSOFT-001 / OQ-5) ──────────────────────────


def _stderr_fallback(
    *,
    event_type: str,
    user_key: str,
    chat_id: int,
    thread_id: UUID | None,
    turn_no: int | None,
    payload: Mapping[str, Any],
    langfuse_trace: str | None,
    latency_ms: int | None,
    error_phase: str,
    exc: BaseException | None = None,
) -> None:
    """Emit a single-line JSON record to stderr.

    Tagged with `"tag": "CONV_LOG_FALLBACK"` so operators can
    `docker logs ... | jq 'select(.tag=="CONV_LOG_FALLBACK")'`.

    @MX:NOTE: PII / 원본 사용자 텍스트 / Pinterest URL 등은 stderr 에 출력하지
    않는다 (security review P1-02). 컨테이너 로그 수집 (CloudWatch 등) 은 PG
    테이블과 다른 보존 정책을 갖고 있고 user_key 기반 DELETE 도 보장 못 하므로
    GDPR 격차를 만들기 때문. metadata (event_type / user_key / chat_id /
    error_phase / error_type) 만 남기고 payload 본문은 size 만 기록한다.
    """
    payload_size = 0
    try:
        payload_size = len(payload) if isinstance(payload, Mapping) else 0
    except Exception:  # noqa: BLE001 — defensive
        payload_size = -1
    record: dict[str, Any] = {
        "tag": "CONV_LOG_FALLBACK",
        "event_type": event_type,
        "user_key": user_key,
        "chat_id": chat_id,
        "thread_id": str(thread_id) if thread_id is not None else None,
        "turn_no": turn_no,
        # payload 본문 대신 카운트만 — REQ-LOG-PRIVACY-001 cascade 보호
        "payload_keys": payload_size,
        "langfuse_trace": langfuse_trace,
        "latency_ms": latency_ms,
        "error_phase": error_phase,
        "ts": datetime.now(tz=UTC).isoformat(),
    }
    if exc is not None:
        record["error_type"] = type(exc).__name__
        # error_msg 는 의도적으로 제외 — exception 본문에 DSN/토큰/내부 호스트가
        # 섞일 수 있어 user_key 기반 GDPR delete 가 닿지 않는 stderr 에 남기지
        # 않는다 (security review P1-01). error_type 만 운영용으로 노출.
    try:
        line = json.dumps(record, default=str)
    except Exception:  # noqa: BLE001 — last-resort, MUST NOT raise
        line = json.dumps(
            {
                "tag": "CONV_LOG_FALLBACK",
                "event_type": event_type,
                "user_key": user_key,
                "payload_keys": payload_size,
                "error_phase": "encode",
            }
        )
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 — extremely unlikely (stderr closed); never raise
        pass


# ── Backend selection ──────────────────────────────────────────────────────


def _backend_is_postgres() -> bool:
    """Read `app.state.conv_log_backend` set by the lifespan probe (LOG-T07).

    Importing FastAPI app eagerly would create a circular dep; use lazy import.
    When unset (test env / probe never ran) → False → stderr fallback path.
    """
    try:
        from app.main import app  # local import to break cycle

        backend = getattr(app.state, "conv_log_backend", None)
        return backend == "postgres"
    except Exception:  # noqa: BLE001
        return False


# ── log_event (async) ──────────────────────────────────────────────────────


async def log_event(
    *,
    event_type: str,
    user_key: str,
    chat_id: int,
    thread_id: UUID | None,
    turn_no: int | None,
    payload: Mapping[str, Any],
    langfuse_trace: str | None = None,
    latency_ms: int | None = None,
    user_id: UUID | None = None,
) -> None:
    """Insert one row into `ai.log_conversation_event`. **Never raises.**

    `user_id` is the real `ai.user_profiles.user_id` for consumer-app turns
    (None for telegram-channel turns). Callers normally omit it and let
    `emit()` fill it from the per-turn context (see `emit()` docstring).

    REQ-LOG-FAILSOFT-001 — any failure (pool acquire / encode / insert) is
    swallowed and re-emitted to stderr as a `CONV_LOG_FALLBACK` line.

    Caller responsibilities:
    - Payload MUST already be truncated (use `emit(...)` which does this).
    - `langfuse_trace` MUST be captured in the *caller* context (use `emit(...)`).
    """
    if not _backend_is_postgres():
        # REQ-LOG-FALLBACK-001 — memory_backend=in_memory silently skips.
        # We still re-route to stderr ONLY when the probe declared "stderr"
        # explicitly (i.e., probe ran AND failed). When no probe ran at all
        # (e.g., test env without lifespan), we just DEBUG log.
        try:
            from app.main import app  # local import

            backend = getattr(app.state, "conv_log_backend", None)
        except Exception:  # noqa: BLE001
            backend = None
        if backend == "stderr":
            _stderr_fallback(
                event_type=event_type,
                user_key=user_key,
                chat_id=chat_id,
                thread_id=thread_id,
                turn_no=turn_no,
                payload=payload,
                langfuse_trace=langfuse_trace,
                latency_ms=latency_ms,
                error_phase="backend_unavailable",
                exc=None,
            )
        else:
            logger.debug(
                "[CONV_LOG][skip] backend=in_memory event_type=%s user_key=%s",
                event_type,
                user_key,
            )
        return

    # Postgres path.
    error_phase = "pool_acquire"
    try:
        from app.providers.db_pool import get_pool

        pool = get_pool()
        error_phase = "encode"
        payload_jsonb = Jsonb(to_jsonable(dict(payload)))
        error_phase = "insert"
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO ai.log_conversation_event (
                    user_key, chat_id, thread_id, turn_no, event_type,
                    payload, langfuse_trace, latency_ms, user_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    user_key,
                    chat_id,
                    thread_id,
                    turn_no,
                    event_type,
                    payload_jsonb,
                    langfuse_trace,
                    latency_ms,
                    user_id,
                ),
            )
            await conn.commit()
    except Exception as exc:  # noqa: BLE001 — never propagate
        _stderr_fallback(
            event_type=event_type,
            user_key=user_key,
            chat_id=chat_id,
            thread_id=thread_id,
            turn_no=turn_no,
            payload=payload,
            langfuse_trace=langfuse_trace,
            latency_ms=latency_ms,
            error_phase=error_phase,
            exc=exc,
        )
        return


# ── emit (sync wrapper) ────────────────────────────────────────────────────


def emit(
    *,
    event_type: str,
    user_key: str,
    chat_id: int,
    thread_id: UUID | None,
    turn_no: int | None,
    payload: Mapping[str, Any],
    langfuse_trace: str | None = None,
    latency_ms: int | None = None,
    user_id: UUID | None = None,
) -> None:
    """Fire-and-forget convenience wrapper.

    REQ-LOG-FIRE-AND-FORGET-001 — schedules `log_event(...)` via
    `asyncio.create_task` so callers do not await DB I/O. Captures
    `langfuse_trace` in the *caller* context (plan §8.2 / R8 mitigation).
    **Never raises** — even when no event loop is running.

    On WeakSet retention: tasks are added to `_IN_FLIGHT` immediately and the
    `done_callback(discard)` evicts them on completion. This prevents Python
    GC from cancelling the pending task before the INSERT actually runs.

    `user_id` (real `ai.user_profiles.user_id`, joinable for `display_name`
    etc.) is normally omitted — the ~50 call sites across graph nodes/tools
    were never threaded to carry it individually, so it is filled here from
    the per-turn context set once via `turn_cost.reset_turn(user_id=...)`
    (SPEC-CONVERSATION-LOG-001 follow-up). Pass it explicitly only for
    call sites that fire outside a turn context (e.g. `_record_call` in
    `turn_cost.py` itself, which already has its own snapshot).
    """
    if user_id is None:
        try:
            from app.observability.turn_cost import get_turn_context

            user_id = get_turn_context().get("user_id")
        except Exception:  # noqa: BLE001
            user_id = None

    # Capture trace_id in caller context FIRST (R8). Never raise from this path.
    if langfuse_trace is None:
        try:
            langfuse_trace = current_langfuse_trace_id()
        except Exception:  # noqa: BLE001
            langfuse_trace = None

    try:
        truncated = _truncate(payload, event_type=event_type)
    except Exception as exc:  # noqa: BLE001 — degrade to empty payload + fallback record
        _stderr_fallback(
            event_type=event_type,
            user_key=user_key,
            chat_id=chat_id,
            thread_id=thread_id,
            turn_no=turn_no,
            payload={"_truncate_failed": True},
            langfuse_trace=langfuse_trace,
            latency_ms=latency_ms,
            error_phase="truncate",
            exc=exc,
        )
        return

    try:
        add_trace_event(event_type, truncated)
    except Exception:  # noqa: BLE001
        pass

    # R9: pool.connection() 은 반드시 pool 이 생성된 loop(memory-pool-loop) 에서
    # 호출해야 한다. FastAPI main loop 에서 직접 호출하면 pool 내부 asyncio
    # primitive 들이 memory-pool-loop 에 바인딩돼 있어 wakeup 신호가 main loop
    # 까지 전달되지 못하고 예외 없이 무한 대기하는 cross-loop hang 이 발생한다.
    # run_coroutine_threadsafe 로 log_event() 전체를 memory-pool-loop 에 제출하면
    # pool.connection() 이 올바른 loop 에서 실행된다.
    try:
        from app.providers import db_pool as _db_pool  # local import — break cycle

        pool_loop = _db_pool.get_loop()
    except Exception:  # noqa: BLE001
        pool_loop = None

    if pool_loop is not None and pool_loop.is_running():
        try:
            fut = asyncio.run_coroutine_threadsafe(
                log_event(
                    event_type=event_type,
                    user_key=user_key,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    turn_no=turn_no,
                    payload=truncated,
                    langfuse_trace=langfuse_trace,
                    latency_ms=latency_ms,
                    user_id=user_id,
                ),
                pool_loop,
            )
        except Exception as exc:  # noqa: BLE001 — never raise
            _stderr_fallback(
                event_type=event_type,
                user_key=user_key,
                chat_id=chat_id,
                thread_id=thread_id,
                turn_no=turn_no,
                payload=truncated,
                langfuse_trace=langfuse_trace,
                latency_ms=latency_ms,
                error_phase="schedule",
                exc=exc,
            )
            return
        _IN_FLIGHT.add(fut)
        fut.add_done_callback(_IN_FLIGHT.discard)
        return

    # pool_loop 미사용 환경(테스트 / pool 미초기화) — 기존 create_task 경로 유지.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — likely sync test harness OR a misuse from a
        # non-async caller. Fall back to stderr so the event is not lost.
        _stderr_fallback(
            event_type=event_type,
            user_key=user_key,
            chat_id=chat_id,
            thread_id=thread_id,
            turn_no=turn_no,
            payload=truncated,
            langfuse_trace=langfuse_trace,
            latency_ms=latency_ms,
            error_phase="no_event_loop",
            exc=None,
        )
        return

    try:
        task = loop.create_task(
            log_event(
                event_type=event_type,
                user_key=user_key,
                chat_id=chat_id,
                thread_id=thread_id,
                turn_no=turn_no,
                payload=truncated,
                langfuse_trace=langfuse_trace,
                latency_ms=latency_ms,
                user_id=user_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 — never raise
        _stderr_fallback(
            event_type=event_type,
            user_key=user_key,
            chat_id=chat_id,
            thread_id=thread_id,
            turn_no=turn_no,
            payload=truncated,
            langfuse_trace=langfuse_trace,
            latency_ms=latency_ms,
            error_phase="schedule",
            exc=exc,
        )
        return

    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)
