"""SPEC-AGENT-V3-REACT / REQ-AGENT-V3-DISLIKE-SCHEMA-001 (Gap4).

Migration 0005 upgrade/downgrade/idempotency on testcontainers Postgres.
Mirrors the test_migration_0004 precedent.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _alembic_config(dsn: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    return cfg


def _column_names(dsn: str, schema: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        return {r[0] for r in cur.fetchall()}


EXPECTED_NEW_COLUMNS = {"disliked_brands_ts", "disliked_keywords_ts"}


def test_migration_0005_adds_two_ts_columns(pg_dsn: str):
    """After conftest's upgrade head, both dislike-ts columns exist."""
    cols = _column_names(pg_dsn, "ai", "user_taste_profile")
    missing = EXPECTED_NEW_COLUMNS - cols
    assert not missing, f"missing columns after upgrade: {missing}"


def test_migration_0005_downgrade_drops_then_reupgrade(pg_dsn: str):
    from alembic import command

    cfg = _alembic_config(pg_dsn)
    # Absolute target "0004" (0005's down_revision) instead of relative "-1":
    # reverts exactly the 0005 dislike-ts columns regardless of how many
    # later migrations exist (a future 0006+ would make "-1" track head and
    # revert the wrong migration).
    command.downgrade(cfg, "0004")
    cols_after_down = _column_names(pg_dsn, "ai", "user_taste_profile")
    overlap = EXPECTED_NEW_COLUMNS & cols_after_down
    assert not overlap, f"columns still present after downgrade: {overlap}"

    command.upgrade(cfg, "head")
    cols_after_up = _column_names(pg_dsn, "ai", "user_taste_profile")
    assert EXPECTED_NEW_COLUMNS.issubset(cols_after_up)


def test_migration_0005_idempotent_re_apply(pg_dsn: str):
    from alembic import command

    cfg = _alembic_config(pg_dsn)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # second call no-op (IF NOT EXISTS)
    cols = _column_names(pg_dsn, "ai", "user_taste_profile")
    assert EXPECTED_NEW_COLUMNS.issubset(cols)


def test_migration_0005_default_empty_object_backward_compat(pg_dsn: str):
    """A row inserted at the pre-0005 schema gets '{}'::jsonb after upgrade —
    the explicit-column SELECT in PostgresTasteProfileStore never hard-fails
    (REQ-AGENT-V3-DISLIKE-SCHEMA-001 / RC5 mitigation)."""
    from alembic import command

    cfg = _alembic_config(pg_dsn)
    # Absolute target "0004" (0005's down_revision) — lands at the genuine
    # pre-0005 schema regardless of head; relative "-1" would only revert
    # the newest migration once a 0006+ exists.
    command.downgrade(cfg, "0004")

    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ai.user_taste_profile (user_key) VALUES (%s) ON CONFLICT (user_key) DO NOTHING",
            ("u:legacy",),
        )
        conn.commit()

    command.upgrade(cfg, "head")

    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT disliked_brands_ts, disliked_keywords_ts FROM ai.user_taste_profile WHERE user_key = %s",
            ("u:legacy",),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == {} and row[1] == {}, "pre-migration row must default to empty jsonb"
