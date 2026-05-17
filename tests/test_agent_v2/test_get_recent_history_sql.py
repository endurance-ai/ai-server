"""Regression guard for the get_recent_history SQL column names.

Incident 2026-05-17: get_recent_history queried a non-existent column `ts`
on ai.log_conversation_event (the real timestamp column is `created_at`,
migration 0003). Every call failed with UndefinedColumn at runtime; V3 Gap1
(memory auto-injection) wraps this tool, so the failure silently neutered
Gap1. The bug survived because no test exercised the real SQL — the V3
suite mocks get_recent_history.

This static guard cross-checks the column identifiers the tool's SQL
references against the actual ai.log_conversation_event schema declared in
migration 0003, with NO database required.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
_TOOL = _REPO / "app" / "agents" / "tools" / "get_recent_history.py"
_MIGRATION = _REPO / "migrations" / "versions" / "0003_create_log_conversation_event.py"


def _log_event_columns() -> set[str]:
    """Column names declared for ai.log_conversation_event in migration 0003."""
    text = _MIGRATION.read_text(encoding="utf-8")
    # Migration 0003 declares the table inside op.execute(\"\"\"...\"\"\"),
    # so the CREATE TABLE body closes with `)` followed by the triple-quote
    # (no trailing semicolon). Match up to that closing paren.
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS ai\.log_conversation_event\s*\((.*?)\)\s*\"\"\"",
        text,
        re.DOTALL,
    )
    assert block, "could not locate log_conversation_event CREATE TABLE in migration 0003"
    cols: set[str] = set()
    for line in block.group(1).splitlines():
        m = re.match(r"\s*([a-z_]+)\s+(text|jsonb|bigint|uuid|integer|timestamptz)", line)
        if m:
            cols.add(m.group(1))
    assert {"event_type", "payload", "created_at", "user_key"} <= cols
    return cols


def test_get_recent_history_sql_columns_exist_in_schema() -> None:
    src = _TOOL.read_text(encoding="utf-8")
    valid = _log_event_columns()

    # The buggy identifier must never come back.
    assert " ts " not in src and " ts," not in src and " ts\n" not in src and "ts DESC" not in src, (
        "get_recent_history references a bare `ts` column; "
        "ai.log_conversation_event has no `ts` column (use created_at)"
    )

    # Every column the SELECT pulls must be a real schema column.
    sel = re.search(r"SELECT\s+(.*?)\s+FROM\s+ai\.log_conversation_event", src)
    assert sel, "could not find the log_conversation_event SELECT in get_recent_history"
    selected = {c.strip() for c in sel.group(1).split(",")}
    unknown = selected - valid
    assert not unknown, f"get_recent_history SELECTs columns absent from schema: {unknown}"

    # The ORDER BY key must also be a real column.
    order = re.search(r"ORDER BY\s+([a-z_]+)\s+DESC", src)
    assert order, "could not find ORDER BY in get_recent_history"
    assert order.group(1) in valid, f"ORDER BY uses unknown column: {order.group(1)}"
    assert "created_at" in selected and order.group(1) == "created_at"
