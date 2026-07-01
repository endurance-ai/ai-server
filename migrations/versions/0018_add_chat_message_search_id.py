"""add search_id column to ai.chat_messages

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-01

An assistant recommendation turn shows a "더보기" (more) button under its
product cards; the button opens the result-set page (GET /v1/results/{search_id}).
The search_id only reaches the client live via the SSE `search` event, so when a
session is restored via GET /v1/chat/sessions/{id}/messages the button cannot be
reconstructed. Add a nullable search_id UUID on the assistant message row (FK to
ai.searches.search_id) so history restore can rebuild the button.

ON DELETE SET NULL: if a search row is deleted independently the message survives
and the button simply disappears (matches the fail-open spirit of
chat_service._persist_search). Both tables already cascade-delete together on
session delete. Idempotent under re-run via IF NOT EXISTS.

Deploy order: migration-first, then code (append_message's new INSERT column and
the list_messages SELECT both reference this column).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai.chat_messages
            ADD COLUMN IF NOT EXISTS search_id UUID
                REFERENCES ai.searches(search_id) ON DELETE SET NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai.chat_messages
            DROP COLUMN IF EXISTS search_id
        """
    )
