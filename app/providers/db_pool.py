"""psycopg3 AsyncConnectionPool singleton for SPEC-MEMORY-001.

Architecture note: the existing `SessionStore` / `TasteProfileStore` Protocols
expose **sync** methods (`get_or_create`, `update`, `delete`) called from
async LangGraph nodes. To keep that Protocol surface unchanged
(REQ-MEMORY-PROTOCOL-001) while doing async Postgres I/O, the pool is opened
on a dedicated background event-loop thread; sync store methods submit coros
to that loop via `asyncio.run_coroutine_threadsafe` and block on `.result()`.
This briefly suspends the caller's coroutine but does NOT block its loop on
other coroutines longer than the underlying DB round-trip.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Any, Final

from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S: Final[float] = 5.0
_PROBE_SQL: Final[str] = "SELECT 1 FROM ai.user_taste_profile LIMIT 0"

_pool: AsyncConnectionPool | None = None
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _sanitize_dsn(dsn: str) -> str:
    """Strip password from a Postgres DSN for safe logging (R11)."""
    if not dsn:
        return ""
    return re.sub(r"(://[^:/?#@]+):[^@]+@", r"\1:***@", dsn)


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("db pool not initialized; call init_pool() during lifespan")
    return _pool


def get_loop() -> asyncio.AbstractEventLoop:
    if _loop is None:
        raise RuntimeError("db pool loop not initialized")
    return _loop


def run_in_pool_loop(coro: Any) -> Any:
    """Submit `coro` to the pool's dedicated loop and block on its result.

    Designed to be called from sync Protocol methods that themselves run on
    the caller's event loop thread. Uses `asyncio.run_coroutine_threadsafe`
    which is safe across threads.
    """
    loop = get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


def _start_loop_thread() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _runner() -> None:
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=_runner, name="memory-pool-loop", daemon=True)
    t.start()
    ready.wait()
    return loop, t


# @MX:ANCHOR: [AUTO] db pool init is the single startup probe; fallback policy depends on it
# @MX:REASON: probes Postgres reachability + schema presence; downstream stores depend on the result
async def init_pool(dsn: str | None = None) -> AsyncConnectionPool:
    """Open the pool, probe, and start the dedicated loop thread.

    Returns the opened pool. Raises on probe failure — caller decides whether
    to fall back (REQ-MEMORY-FALLBACK-001/002).
    """
    global _pool, _loop, _loop_thread
    target_dsn = dsn or settings.DB_DSN
    if not target_dsn:
        raise RuntimeError("DB_DSN is empty; cannot initialize Postgres pool")

    safe_dsn = _sanitize_dsn(target_dsn)
    logger.info(
        "[MEMORY][startup] opening pool dsn=%s min=%d max=%d",
        safe_dsn,
        settings.MEMORY_POOL_MIN_SIZE,
        settings.MEMORY_POOL_MAX_SIZE,
    )

    # Spin up the dedicated loop thread first; the pool will live on that loop.
    loop, t = _start_loop_thread()

    async def _create_and_probe() -> AsyncConnectionPool:
        pool = AsyncConnectionPool(
            conninfo=target_dsn,
            min_size=settings.MEMORY_POOL_MIN_SIZE,
            max_size=settings.MEMORY_POOL_MAX_SIZE,
            kwargs={"connect_timeout": int(_PROBE_TIMEOUT_S)},
            open=False,
        )
        await pool.open(wait=False)

        async def _probe() -> None:
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_PROBE_SQL)

        try:
            await asyncio.wait_for(_probe(), timeout=_PROBE_TIMEOUT_S + 1.0)
        except Exception:
            try:
                await pool.close()
            except Exception:
                pass
            raise
        return pool

    try:
        fut = asyncio.run_coroutine_threadsafe(_create_and_probe(), loop)
        # Outer guard: hard cap at 2 * probe timeout for the entire init+probe sequence.
        pool = await asyncio.get_event_loop().run_in_executor(None, lambda: fut.result(timeout=_PROBE_TIMEOUT_S + 1.0))
    except Exception:
        # Shutdown the loop thread on probe failure.
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=2.0)
        raise

    _pool = pool
    _loop = loop
    _loop_thread = t
    logger.info("[MEMORY][startup] pool probe ok backend=postgres")
    return pool


async def close_pool() -> None:
    """Drain and close the pool. Idempotent."""
    global _pool, _loop, _loop_thread
    if _pool is not None and _loop is not None:
        try:
            fut = asyncio.run_coroutine_threadsafe(_pool.close(), _loop)
            await asyncio.get_event_loop().run_in_executor(None, lambda: fut.result(timeout=5.0))
        except Exception:
            logger.exception("error closing pool")
    if _loop is not None:
        try:
            _loop.call_soon_threadsafe(_loop.stop)
        except Exception:
            pass
    if _loop_thread is not None:
        _loop_thread.join(timeout=2.0)
    _pool = None
    _loop = None
    _loop_thread = None


def reset_pool_for_test(
    pool: AsyncConnectionPool | None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Test-only hook."""
    global _pool, _loop
    _pool = pool
    _loop = loop
