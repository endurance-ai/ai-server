"""Hermetic Postgres fixture for SPEC-MEMORY-001 tests.

Spins up a single Postgres container via testcontainers per test session,
runs `alembic upgrade head` against it, and exposes a fresh truncated state
to every test function.

Tests in this directory skip when Docker is unavailable.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator

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
    """Create the `ai` schema before running migrations.

    On dev-app this is done once by a superuser (ai_user lacks DB-level CREATE).
    In tests, the testcontainers default user IS the DB owner and can do it.
    Migrations themselves never `CREATE SCHEMA` to preserve dev-app permissions.
    """
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

    os.environ["DB_DSN"] = pg_dsn
    from app.core import config as cfg_mod

    cfg_mod.get_settings.cache_clear()
    cfg_mod.settings = cfg_mod.get_settings()
    db_pool.settings = cfg_mod.settings

    await db_pool.init_pool(pg_dsn)
    yield
    await db_pool.close_pool()


@pytest_asyncio.fixture(autouse=True)
async def _truncate_tables(pool_initialized: None) -> AsyncGenerator[None]:
    from app.providers.db_pool import get_pool, run_in_pool_loop

    async def _truncate() -> None:
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute("TRUNCATE ai.user_session, ai.user_taste_profile")
            await conn.commit()

    run_in_pool_loop(_truncate())
    yield
