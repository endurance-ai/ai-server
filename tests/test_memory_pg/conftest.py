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

    # pgvector/pgvector:pg16 = official postgres:16 + pgvector .so files preinstalled.
    # Required by migration 0007 which creates `embedding_cache_text (embedding vector(768))`.
    # 2026-05-20: switched from postgres:16-alpine → pgvector/pgvector:pg16 (no alpine variant).
    with PostgresContainer("pgvector/pgvector:pg16") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)
        os.environ["DB_DSN"] = dsn
        _bootstrap_ai_schema(dsn)
        _alembic_upgrade(dsn)
        yield pg


def _bootstrap_ai_schema(dsn: str) -> None:
    """Create the `ai` schema + pgvector extension before running migrations.

    On dev-app these are done once by a superuser (ai_user lacks DB-level CREATE).
    In tests, the testcontainers default user IS the DB owner and can do it.
    Migrations themselves never `CREATE SCHEMA` / `CREATE EXTENSION` to preserve
    dev-app permissions — migration 0007 (`embedding_cache_text vector(768)`)
    relies on the extension being pre-installed by this bootstrap.

    autocommit=True 로 CREATE EXTENSION 을 명시적 commit 없이 즉시 반영
    (psycopg context manager 의 rollback-on-exception 경로 우회).
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS ai")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        # 검증: 정말 등록됐는지 catalog 조회 — 등록 실패 시 명확한 에러로 변환.
        cur.execute("SELECT extname FROM pg_extension WHERE extname='vector'")
        if cur.fetchone() is None:
            raise RuntimeError(
                "pgvector 확장 활성화 실패 — pgvector/pgvector:pg16 이미지 또는 testcontainers 설정 점검"
            )


def _alembic_upgrade(dsn: str) -> None:
    """Upgrade 마이그레이션 — 0007 (vector 컬럼) 전후로 분리해 race 회피.

    CI testcontainers (pgvector/pgvector:pg16) 에서 같은 alembic transaction
    안에 CREATE EXTENSION + CREATE TABLE vector 가 묶이면 두 번째 statement 가
    type 을 못 찾는 race 가 재현됨. 0006 까지 먼저 올린 뒤, 별도 connection
    으로 CREATE EXTENSION 을 autocommit, 그 다음 head 까지 올린다.
    """
    import psycopg
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))

    command.upgrade(cfg, "0006")

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def pg_dsn(pg_container) -> str:
    return pg_container.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)


@pytest_asyncio.fixture
async def pool_initialized(pg_dsn: str) -> AsyncGenerator[None]:
    # NOTE: do NOT reassign `app.core.config.settings` or any module's
    # `settings` reference — that would split the singleton into multiple
    # instances and break every test in the wider suite that relies on
    # `monkeypatch.setattr(settings, ...)` reaching the same object held
    # by other modules' `from ... import settings` references.
    # `init_pool` accepts an explicit DSN so we don't have to mutate settings.
    from app.providers import db_pool

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
