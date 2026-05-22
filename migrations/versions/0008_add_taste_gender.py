"""add gender column to user_taste_profile

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-22

SPEC-GENDER-PIN-001 (260522). Adds a single nullable TEXT column
`gender` to ai.user_taste_profile so a user's pinned gender ('men' /
'women' / 'unisex') persists cross-session. NULL = not yet pinned (the
agent asks via a one-time gender card on the first genderless text search).
Idempotent under re-run via IF NOT EXISTS.

Deploy order: migration-first, then code (the explicit-column SELECT in
`taste_profile_pg._aget_or_create` requires this column to exist).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET search_path TO ai")
    op.execute(
        """
        ALTER TABLE ai.user_taste_profile
            ADD COLUMN IF NOT EXISTS gender TEXT
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai.user_taste_profile
            DROP COLUMN IF EXISTS gender
        """
    )
