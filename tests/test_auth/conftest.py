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
        # Minimal crawler tables needed by product endpoints
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.brand_nodes (
                id          BIGSERIAL PRIMARY KEY,
                brand_name  TEXT NOT NULL,
                brand_name_normalized TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.products (
                id             BIGSERIAL PRIMARY KEY,
                brand          TEXT NOT NULL,
                name           TEXT NOT NULL,
                category       TEXT,
                subcategory    TEXT,
                price          NUMERIC,
                original_price NUMERIC,
                sale_price     NUMERIC,
                source_currency TEXT DEFAULT 'KRW',
                source_price   NUMERIC,
                image_url      TEXT NOT NULL DEFAULT '',
                images         TEXT[],
                product_url    TEXT NOT NULL UNIQUE,
                in_stock       BOOLEAN NOT NULL DEFAULT TRUE,
                platform       TEXT NOT NULL DEFAULT '',
                gender         TEXT[],
                description    TEXT,
                color          TEXT,
                tags           TEXT[],
                product_code   TEXT,
                brand_node_id  BIGINT REFERENCES public.brand_nodes(id),
                crawled_at     TIMESTAMPTZ DEFAULT now(),
                last_seen_at   TIMESTAMPTZ DEFAULT now(),
                updated_at     TIMESTAMPTZ DEFAULT now()
            )
        """)


def _migrate(dsn: str) -> None:
    import logging

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")

    # alembic.ini's fileConfig runs with disable_existing_loggers=True (the default),
    # which individually disables every existing logger not listed in alembic.ini.
    # Re-enable all app.* loggers so caplog-based tests in other modules still work.
    for name, logger in logging.Logger.manager.loggerDict.items():
        if name.startswith("app") and isinstance(logger, logging.Logger):
            logger.disabled = False


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
                ai.feedbacks,
                ai.devices,
                ai.legal_consents,
                ai.user_profiles,
                ai.user_session,
                ai.user_taste_profile
            CASCADE
            """
        )
        await cur.execute("TRUNCATE public.products, public.brand_nodes RESTART IDENTITY CASCADE")
        await conn.commit()


@pytest_asyncio.fixture
async def client(pool) -> AsyncGenerator[AsyncClient]:
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
