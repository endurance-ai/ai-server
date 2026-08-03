"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-MIGRATION-001 + REQ-ONBOARD-PINTEREST-007.

Migration 0004 upgrade/downgrade/idempotency on testcontainers Postgres.
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


EXPECTED_NEW_COLUMNS = {
    "onboarded_at",
    "onboard_stage",
    "onboard_selections",
    "onboard_card_message_id",
    "last_pinterest_scrape_url",
    "last_pinterest_scrape_at",
    "last_pinterest_pins",
}


def test_migration_0004_adds_all_7_columns(pg_dsn: str):
    """After conftest's upgrade head, all 7 onboarding columns exist on user_session."""
    cols = _column_names(pg_dsn, "ai", "user_session")
    missing = EXPECTED_NEW_COLUMNS - cols
    assert not missing, f"missing columns after upgrade: {missing}"


def test_migration_0004_downgrade_drops_all_7_columns(pg_dsn: str):
    """Downgrade to 0003 removes the 7 columns; re-upgrade restores them.

    Uses the absolute target "0003" (0004's down_revision) rather than the
    relative "-1" so the test stays correct regardless of how many later
    migrations exist beyond 0004 (e.g. 0005+). With "-1" the target would
    track the Alembic head and only revert the newest migration.
    """
    from alembic import command

    cfg = _alembic_config(pg_dsn)

    command.downgrade(cfg, "0003")
    cols_after_down = _column_names(pg_dsn, "ai", "user_session")
    overlap = EXPECTED_NEW_COLUMNS & cols_after_down
    assert not overlap, f"columns still present after downgrade: {overlap}"

    # Re-upgrade so subsequent tests in the session see the columns again.
    command.upgrade(cfg, "head")
    cols_after_up = _column_names(pg_dsn, "ai", "user_session")
    assert EXPECTED_NEW_COLUMNS.issubset(cols_after_up)


def test_migration_0004_idempotent_re_apply(pg_dsn: str):
    """Re-running upgrade head twice does not raise (IF NOT EXISTS)."""
    from alembic import command

    cfg = _alembic_config(pg_dsn)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # second call should be a no-op
    cols = _column_names(pg_dsn, "ai", "user_session")
    assert EXPECTED_NEW_COLUMNS.issubset(cols)


def test_migration_0004_backfills_existing_onboarded_at(pg_dsn: str):
    """Existing rows are stamped with `onboarded_at = now()` per REQ-ONBOARD-MIGRATION-001.

    Insert a row at the pre-migration schema (downgrade first), then upgrade and
    verify the backfill ran.
    """
    from alembic import command

    cfg = _alembic_config(pg_dsn)
    # Start from the 0003 state (absolute target — 0004's down_revision —
    # so we land at the genuine pre-0004 schema regardless of later
    # migrations; "-1" would only revert the current head).
    command.downgrade(cfg, "0003")

    # Insert a row using the pre-0004 schema.
    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ai.user_session (chat_id, state) VALUES (%s, %s) ON CONFLICT (chat_id) DO NOTHING",
            (424242, "idle"),
        )
        conn.commit()

    # Upgrade to 0004 — the migration backfills `onboarded_at = now()` for existing rows.
    command.upgrade(cfg, "head")

    with psycopg.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT onboarded_at FROM ai.user_session WHERE chat_id = %s", (424242,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is not None, "existing row should have onboarded_at backfilled"
