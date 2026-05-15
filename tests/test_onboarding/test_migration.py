"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-MIGRATION-001+002 — alembic 0004 verification.

Spins up a Postgres container, runs migrations up to revision 0003, inserts a
sentinel `ai.user_session` row, runs `upgrade 0004`, asserts the 7 new columns
exist + the sentinel row was backfilled with `onboarded_at` non-NULL, then
runs `downgrade -1` and confirms the new columns are removed.

Skipped when Docker is unavailable (same gate as `tests/test_memory_pg/`).

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]

        client = docker.from_env()
        client.ping()
        return True
    except Exception:  # noqa: BLE001
        return False


_DOCKER_OK = _docker_available()


_NEW_COLUMNS = {
    "onboarded_at",
    "onboard_stage",
    "onboard_selections",
    "onboard_card_message_id",
    "last_pinterest_scrape_url",
    "last_pinterest_scrape_at",
    "last_pinterest_pins",
}


@pytest.fixture(scope="module")
def isolated_pg() -> Generator:
    """Module-scoped container — separate from `test_memory_pg/conftest.py`
    fixture so we can step the migration manually (0003 → 0004 → 0003)."""
    if not _DOCKER_OK:
        pytest.skip("Docker unavailable; skipping migration testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)
        os.environ["DB_DSN"] = dsn
        _bootstrap_ai_schema(dsn)
        yield dsn


def _bootstrap_ai_schema(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS ai")
        conn.commit()


def _alembic_upgrade(dsn: str, target: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, target)


def _alembic_downgrade(dsn: str, target: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.downgrade(cfg, target)


def _columns_of(dsn: str, table: str) -> set[str]:
    import psycopg

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'ai' AND table_name = %s
            """,
            (table,),
        )
        return {row[0] for row in cur.fetchall()}


def test_migration_0004_adds_seven_columns_and_backfills(isolated_pg: str) -> None:
    """REQ-ONBOARD-MIGRATION-001 — upgrade head installs all 7 new columns
    AND backfills `onboarded_at` for pre-existing user_session rows.
    """
    import psycopg

    dsn = isolated_pg
    # Upgrade to 0003 first so we have user_session schema without the new cols.
    _alembic_upgrade(dsn, "0003")
    pre_cols = _columns_of(dsn, "user_session")
    assert not (_NEW_COLUMNS & pre_cols), (
        f"unexpected new columns already present at 0003: {sorted(_NEW_COLUMNS & pre_cols)}"
    )

    # Seed a sentinel row at 0003 — backfill must populate onboarded_at for it.
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ai.user_session (chat_id, state) VALUES (%s, %s)",
            (999_001, "IDLE"),
        )
        conn.commit()

    # Now move to head — should add all 7 columns + backfill.
    _alembic_upgrade(dsn, "head")
    post_cols = _columns_of(dsn, "user_session")
    missing = _NEW_COLUMNS - post_cols
    assert not missing, f"migration 0004 failed to add columns: {sorted(missing)}"

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT onboarded_at FROM ai.user_session WHERE chat_id = %s",
            (999_001,),
        )
        row = cur.fetchone()
        assert row is not None and row[0] is not None, (
            "REQ-ONBOARD-MIGRATION-001 — onboarded_at backfill missing on existing row"
        )


def test_migration_0004_downgrade_removes_new_columns(isolated_pg: str) -> None:
    """REQ-ONBOARD-MIGRATION-001 — downgrade is clean."""
    dsn = isolated_pg
    # Ensure we're at head (the previous test left us there).
    _alembic_upgrade(dsn, "head")
    assert _NEW_COLUMNS <= _columns_of(dsn, "user_session")

    _alembic_downgrade(dsn, "0003")
    post = _columns_of(dsn, "user_session")
    overlap = _NEW_COLUMNS & post
    assert not overlap, f"downgrade left orphan columns: {sorted(overlap)}"

    # Restore head so subsequent tests / fixtures behave as expected.
    _alembic_upgrade(dsn, "head")
