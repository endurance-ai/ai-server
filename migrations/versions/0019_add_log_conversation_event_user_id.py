"""add user_id column to ai.log_conversation_event

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-03

`ai.log_conversation_event` predates the consumer-app auth model (migration
0009) and only carries `user_key` (telegram-era string: `u:<tg_id>` or
`c:<synthetic_chat_id>`). `ai.user_taste_profile` already got a nullable
`user_id UUID` FK to `ai.user_profiles` in migration 0009 for the same
gradual-migration reason; `log_conversation_event` was missed.

Without this column, joining conversation/turn analytics to real app users
(e.g. to pull `display_name`) requires re-deriving `synthetic_chat_id` from
`user_id` in Python (`chat_service._user_id_to_chat_id`), which cannot be
replicated in SQL. Add the column directly so app-flow rows can be written
with the real `user_id` and joined with `ai.user_profiles USING (user_id)`.

Nullable + ON DELETE SET NULL: telegram-channel rows (no app user_profiles
row) keep user_id=NULL, matching the user_key fallback split. Deleting a
user_profiles row must not cascade-delete historical log rows.

Deploy order: migration-first, then code (`app/observability/turn_cost.py` +
`app/observability/conversation_log.py` start writing `user_id` once this
column exists).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai.log_conversation_event
            ADD COLUMN IF NOT EXISTS user_id UUID
                REFERENCES ai.user_profiles(user_id) ON DELETE SET NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_log_conv_user_id_time ON ai.log_conversation_event (user_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ai.idx_log_conv_user_id_time")
    op.execute("ALTER TABLE ai.log_conversation_event DROP COLUMN IF EXISTS user_id")
