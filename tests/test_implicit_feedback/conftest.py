"""SPEC-IMPLICIT-FB-001 — shared fixtures.

Reuses the Postgres testcontainer from `tests/test_memory_pg/conftest.py` so we
share a single container per test session. Tests that don't need Postgres
(config, fallback unit tests) skip the pool entirely.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio

# Re-export the Docker-aware session-scoped pg_container fixture.
from tests.test_memory_pg.conftest import _DOCKER_OK, pg_container  # noqa: F401, F811


@dataclass
class FakeCandidate:
    """Lightweight Candidate-shaped object for tests."""

    id: str
    brand: str = ""
    name: str = ""
    subcategory: str = ""
    keywords: list[str] = field(default_factory=list)


@pytest.fixture
def fake_candidates() -> list[FakeCandidate]:
    return [
        FakeCandidate(id="p1", brand="Ami", name="Heart Tee", subcategory="tee", keywords=["oversized", "cotton"]),
        FakeCandidate(id="p2", brand="Acne", name="Denim Pants", subcategory="denim", keywords=["wide", "indigo"]),
        FakeCandidate(id="p3", brand="Maison", name="Wool Coat", subcategory="coat", keywords=["wool", "long"]),
    ]


@pytest_asyncio.fixture
async def pg_pool_with_impressions(pg_container) -> AsyncGenerator[None]:  # noqa: F811
    """Init pool, truncate card_impression + taste/session, register backend flag."""
    from app.channels.implicit_feedback import (
        reset_warn_once_for_tests,
        set_backend_is_postgres,
    )
    from app.providers import db_pool
    from app.providers.db_pool import get_pool, run_in_pool_loop

    dsn = pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)
    await db_pool.init_pool(dsn)
    set_backend_is_postgres(True)
    reset_warn_once_for_tests()

    async def _truncate() -> None:
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute("TRUNCATE ai.card_impression, ai.user_session, ai.user_taste_profile RESTART IDENTITY")
            await conn.commit()

    run_in_pool_loop(_truncate())

    yield

    set_backend_is_postgres(False)
    await db_pool.close_pool()


@pytest.fixture
def in_memory_taste_store(monkeypatch):
    """Force an in-memory TasteProfile store for fast unit tests."""
    from app.channels import taste_profile as _tp

    store = _tp.InMemoryTasteProfileStore()
    monkeypatch.setattr(_tp, "_store", store)
    return store
