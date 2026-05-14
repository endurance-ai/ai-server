"""Testcontainers-backed fixtures for SPEC-CONVERSATION-LOG-001 Phase 2 tests.

Mirrors `tests/test_memory_pg/conftest.py` but truncates
`ai.log_conversation_event` between tests. Skips when Docker is unavailable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from types import SimpleNamespace

import pytest
import pytest_asyncio


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


_DOCKER_OK = _docker_available()


@pytest.fixture(scope="session")
def pg_container() -> Generator:
    if not _DOCKER_OK:
        pytest.skip("Docker daemon unavailable; skipping testcontainers-backed tests")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)
        os.environ["DB_DSN"] = dsn
        _bootstrap_ai_schema(dsn)
        _alembic_upgrade(dsn)
        yield pg


def _bootstrap_ai_schema(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS ai")
        conn.commit()


def _alembic_upgrade(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def pg_dsn(pg_container) -> str:
    return pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)


@pytest_asyncio.fixture
async def pool_initialized(pg_dsn: str) -> AsyncGenerator[None]:
    from app.providers import db_pool

    await db_pool.init_pool(pg_dsn)
    yield
    await db_pool.close_pool()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_log_table(pool_initialized: None) -> AsyncGenerator[None]:
    from app.providers.db_pool import get_pool, run_in_pool_loop

    async def _truncate() -> None:
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute("TRUNCATE ai.log_conversation_event RESTART IDENTITY")
            await conn.commit()

    run_in_pool_loop(_truncate())
    yield


@pytest_asyncio.fixture
async def conv_log_backend_postgres(monkeypatch, pool_initialized: None):
    """Set app.state.conv_log_backend = 'postgres' so `emit()` writes rows."""
    fake_app = SimpleNamespace(state=SimpleNamespace(conv_log_backend="postgres"))
    monkeypatch.setattr("app.main.app", fake_app, raising=False)
    yield
