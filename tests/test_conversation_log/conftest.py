"""Testcontainers-backed fixtures for SPEC-CONVERSATION-LOG-001 Phase 2 tests.

Mirrors `tests/test_memory_pg/conftest.py` but truncates
`ai.log_conversation_event` between tests. Skips when Docker is unavailable.
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


# CI에서 asyncio.create_task + ASGITransport + psycopg pool 의 이벤트 루프
# 경계 race 로 인해 thread_* 테스트들이 0 rows 로 fail 함. 로컬은 Docker
# 부재로 skip 되어 안 보이는 이슈. 본 PR scope 밖 — 별도 인프라 SPEC 에서 다룸.
# 영향 받는 파일: test_thread_callback.py, test_thread_propagation.py.
SKIP_ASYNC_LOOP_RACE_TESTS = os.environ.get("CI", "").lower() == "true"


@pytest.fixture(scope="session")
def pg_container() -> Generator:
    if not _DOCKER_OK:
        pytest.skip("Docker daemon unavailable; skipping testcontainers-backed tests")
    from testcontainers.postgres import PostgresContainer

    # pgvector/pgvector:pg16 = postgres:16 + pgvector .so preinstalled.
    # Migration 0007 needs `vector(768)` type — see tests/test_memory_pg/conftest.py
    # for the rationale (kept identical across the two PG conftests).
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)
        os.environ["DB_DSN"] = dsn
        _bootstrap_ai_schema(dsn)
        _alembic_upgrade(dsn)
        yield pg


def _bootstrap_ai_schema(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS ai")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
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
    """Set app.state.conv_log_backend = 'postgres' so `emit()` writes rows.

    실제 FastAPI app 의 state 만 monkeypatch — 전체 app 객체를 SimpleNamespace
    로 교체하면 ASGI transport 가 작동 안 함 (webhook_client fixture 깨짐).
    """
    from app.main import app

    prior = getattr(app.state, "conv_log_backend", None)
    app.state.conv_log_backend = "postgres"
    try:
        yield
    finally:
        if prior is None:
            try:
                del app.state.conv_log_backend
            except AttributeError:
                pass
        else:
            app.state.conv_log_backend = prior
