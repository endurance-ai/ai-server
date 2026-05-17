"""SPEC-CONVERSATION-LOG-001 / LOG-T25 — REQ-LOG-RETENTION-001 no-auto-cleanup.

`app/infrastructure/memory/session_pg.py` 에서 `log_conversation_event` 라는 문자열이
나타나지 않음을 AST + raw text scan 으로 검증.

REQ-LOG-RETENTION-001: 세션 TTL cleanup 이 conversation event log 를 cascade
삭제해서는 안 된다. 본 SPEC 의 row 들은 **영구 보존**.

@MX:SPEC: SPEC-CONVERSATION-LOG-001
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SESSION_PG_PATH = REPO_ROOT / "app" / "infrastructure" / "memory" / "session_pg.py"


# Override autouse Docker-dependent fixture from conftest — pure AST scan.
@pytest.fixture(autouse=True)
def _truncate_log_table():
    yield


def test_session_pg_has_no_log_conversation_event_reference():
    """AST + raw text scan: `session_pg.py` MUST NOT reference `log_conversation_event`.

    Failure modes guarded against:
      (a) string literal `"log_conversation_event"` (would imply cascading DELETE).
      (b) attribute / name `log_conversation_event` (import / alias).
      (c) substring inside a multi-line SQL statement.
    """
    assert SESSION_PG_PATH.exists(), f"Expected {SESSION_PG_PATH}"
    source = SESSION_PG_PATH.read_text(encoding="utf-8")

    # Raw text scan — catches any occurrence including comments + SQL strings.
    assert "log_conversation_event" not in source, (
        "session_pg.py references `log_conversation_event` — violates "
        "REQ-LOG-RETENTION-001 (no session-TTL cascade delete)."
    )

    # AST scan — defensive double-check on Constant / Name / Attribute nodes.
    tree = ast.parse(source, filename=str(SESSION_PG_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "log_conversation_event" not in node.value
        if isinstance(node, ast.Name):
            assert node.id != "log_conversation_event"
        if isinstance(node, ast.Attribute):
            assert node.attr != "log_conversation_event"
