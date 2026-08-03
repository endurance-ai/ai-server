"""SPEC-CONVERSATION-LOG-001 / LOG-T25 — REQ-LOG-MIGRATION-001 alembic ↑/↓.

Migration 0003 의 upgrade / downgrade / idempotent re-apply 를 검증한다.
testcontainers-postgres 필요.

@MX:SPEC: SPEC-CONVERSATION-LOG-001
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


def _table_exists(dsn: str, schema: str, table: str) -> bool:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        )
        return cur.fetchone() is not None


def _list_indexes(dsn: str, schema: str, table: str) -> list[str]:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s ORDER BY indexname",
            (schema, table),
        )
        return [r[0] for r in cur.fetchall()]


def test_migration_0003_upgrade_creates_table_and_indexes(pg_dsn: str):
    """After conftest's `_alembic_upgrade(head)`, the table + 4 indexes exist."""
    assert _table_exists(pg_dsn, "ai", "log_conversation_event")
    idx = _list_indexes(pg_dsn, "ai", "log_conversation_event")
    expected = {
        "idx_log_conv_user_time",
        "idx_log_conv_thread",
        "idx_log_conv_event_type",
        "idx_log_conv_payload_gin",
    }
    assert expected.issubset(set(idx)), f"Missing indexes; found {idx}"


def test_migration_0003_downgrade_and_reupgrade_is_idempotent(pg_dsn: str):
    """`alembic downgrade -1` drops the table; re-`upgrade head` re-creates it.

    Note: After downgrade we re-upgrade so subsequent tests (using the
    session-scoped `pg_container`) still see the table.
    """
    from alembic import command

    cfg = _alembic_config(pg_dsn)

    # Downgrade to 0002 explicitly — head is now 0004 (ONBOARD migration added).
    # `-1` would only undo 0004 (onboarded_at column), not the LOG table.
    command.downgrade(cfg, "0002")
    assert not _table_exists(pg_dsn, "ai", "log_conversation_event"), "Table should be dropped after downgrade to 0002"

    # Re-upgrade to head — should be idempotent.
    command.upgrade(cfg, "head")
    assert _table_exists(pg_dsn, "ai", "log_conversation_event"), "Table should re-appear after upgrade head"

    # Indexes should be back too.
    idx = _list_indexes(pg_dsn, "ai", "log_conversation_event")
    assert "idx_log_conv_payload_gin" in idx
