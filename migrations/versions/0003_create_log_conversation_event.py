"""create log_conversation_event table

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-14

SPEC-CONVERSATION-LOG-001 / REQ-LOG-MIGRATION-001 — adds
`ai.log_conversation_event` with 10 columns + 4 indexes (1 GIN with jsonb_ops).
Idempotent under re-run. No FOREIGN KEY (R3 — independence from user_session).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET search_path TO ai")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.log_conversation_event (
            id              bigserial PRIMARY KEY,
            user_key        text NOT NULL,
            chat_id         bigint NOT NULL,
            thread_id       uuid,
            turn_no         integer,
            event_type      text NOT NULL,
            payload         jsonb NOT NULL,
            langfuse_trace  text,
            latency_ms      integer,
            created_at      timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_log_conv_user_time ON ai.log_conversation_event (user_key, created_at DESC)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_log_conv_thread ON ai.log_conversation_event (thread_id, turn_no)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_log_conv_event_type ON ai.log_conversation_event (event_type, created_at DESC)"
    )
    # REQ-LOG-MIGRATION-001 acceptance — default `jsonb_ops` (NOT jsonb_path_ops)
    # so @>, ?, ?&, ?| all supported.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_log_conv_payload_gin ON ai.log_conversation_event USING GIN (payload jsonb_ops)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.log_conversation_event")
