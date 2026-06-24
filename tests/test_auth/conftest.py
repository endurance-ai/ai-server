"""Testcontainers Postgres + JWT secret fixture for auth API tests."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


_DOCKER_OK = _docker_available()


@pytest.fixture(scope="session")
def pg_container() -> Generator:
    if not _DOCKER_OK:
        pytest.skip("Docker unavailable")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)
        os.environ["DB_DSN"] = dsn
        os.environ["JWT_SECRET"] = "test-jwt-secret-must-be-at-least-32-chars!!"
        _bootstrap(dsn)
        _migrate(dsn)
        yield pg


def _bootstrap(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS ai")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")


def _migrate(dsn: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def pg_dsn(pg_container) -> str:
    return pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)


@pytest_asyncio.fixture
async def pool(pg_dsn: str) -> AsyncGenerator[None]:
    from app.providers import db_pool

    await db_pool.init_pool(pg_dsn)
    yield db_pool.get_pool()
    await db_pool.close_pool()


@pytest_asyncio.fixture(autouse=True)
async def _truncate(pool) -> AsyncGenerator[None]:
    yield
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            TRUNCATE
                ai.chat_messages,
                ai.chat_sessions,
                ai.refresh_tokens,
                ai.user_profiles,
                ai.user_session,
                ai.user_taste_profile
            CASCADE
            """
        )
        await conn.commit()


@pytest_asyncio.fixture
async def client(pool) -> AsyncGenerator[AsyncClient]:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
