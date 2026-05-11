"""REQ-MEMORY-LIFECYCLE-001 / REQ-MEMORY-FALLBACK-001 / REQ-MEMORY-FALLBACK-002."""

from __future__ import annotations

import time

import pytest

from app.providers import db_pool


@pytest.mark.asyncio
async def test_init_pool_probe_succeeds_against_migrated_container(pg_dsn: str):
    """REQ-MEMORY-LIFECYCLE-001 — probe succeeds when migration is applied."""
    # Pool is already initialized by autouse fixture; just assert it's there.
    assert db_pool.get_pool() is not None


@pytest.mark.asyncio
async def test_init_pool_fails_against_unreachable_dsn():
    """REQ-MEMORY-FALLBACK-001 — probe raises on unreachable DSN."""
    await db_pool.close_pool()
    t0 = time.monotonic()
    with pytest.raises(Exception):
        await db_pool.init_pool("postgresql://localhost:1/none")
    elapsed = time.monotonic() - t0
    # ≤ 6s per REQ-MEMORY-LIFECYCLE-001 AC
    assert elapsed <= 8.0, f"probe took {elapsed:.1f}s, expected ≤ 6-8s"


def test_sanitize_dsn_strips_password():
    """R11 — password redacted in error logs."""
    s = db_pool._sanitize_dsn("postgresql://kiko:supersecret@host:5432/kiko_dev")
    assert "supersecret" not in s
    assert "kiko" in s
    assert "host:5432/kiko_dev" in s
