"""SPEC-MODAL-KEEP-WARM-001 — Modal keep-warm background loop tests.

Verifies that `_modal_keep_warm_loop`:
- Calls `EmbedProvider.check_connection` on each iteration.
- Sleeps for `MODAL_KEEP_WARM_INTERVAL_S` between iterations (clamped ≥30s at read).
- Fails open: an exception in one ping does NOT crash the loop.
- Exits cleanly on `CancelledError`.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_keep_warm_pings_check_connection(monkeypatch):
    """Loop calls check_connection at least once when given time to run."""
    from app import main
    from app.providers.embedding import EmbedProvider

    # 30s clamp still holds — we can't make it fire faster. Instead,
    # intercept asyncio.sleep INSIDE the loop to return immediately, then
    # cancel after one iteration.
    ping = AsyncMock(return_value=True)
    monkeypatch.setattr(EmbedProvider, "check_connection", ping)

    slept = []
    real_sleep = asyncio.sleep

    async def fast_sleep(secs: float) -> None:
        slept.append(secs)
        # Yield control so the outer task can cancel us.
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(main._modal_keep_warm_loop())
    # Let the loop tick a couple of times.
    await real_sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert ping.await_count >= 1, "check_connection should have been awaited at least once"
    # The clamp keeps the sleep at ≥30s regardless of config value.
    assert all(s >= 30 for s in slept), f"sleep intervals should respect the 30s clamp, got {slept}"


@pytest.mark.asyncio
async def test_keep_warm_survives_ping_failure(monkeypatch):
    """A single `check_connection` raise must NOT crash the loop — the next
    tick should still fire. This is the whole point of the fail-open contract
    (Modal being down is a separate alerting concern).
    """
    from app import main
    from app.providers.embedding import EmbedProvider

    call_count = 0

    async def flaky_ping() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated modal outage")
        return True

    monkeypatch.setattr(EmbedProvider, "check_connection", flaky_ping)
    real_sleep = asyncio.sleep

    async def fast_sleep(secs: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(main.asyncio, "sleep", fast_sleep)

    task = asyncio.create_task(main._modal_keep_warm_loop())
    await real_sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count >= 2, "loop should have iterated past the initial failure"


@pytest.mark.asyncio
async def test_keep_warm_cancels_cleanly(monkeypatch):
    """`task.cancel()` should terminate the loop within one scheduler tick,
    NOT hang the shutdown path.
    """
    from app import main
    from app.providers.embedding import EmbedProvider

    monkeypatch.setattr(EmbedProvider, "check_connection", AsyncMock(return_value=True))

    task = asyncio.create_task(main._modal_keep_warm_loop())
    await asyncio.sleep(0)  # yield to let the loop start
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except asyncio.CancelledError:
        pass
    except TimeoutError:
        pytest.fail("keep-warm loop did not honor cancellation within 1s")

    assert task.done()
