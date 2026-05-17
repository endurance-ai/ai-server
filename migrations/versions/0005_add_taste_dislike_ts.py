"""add cross-thread dislike timestamp columns to user_taste_profile

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-17

SPEC-AGENT-V3-REACT / REQ-AGENT-V3-DISLIKE-SCHEMA-001 (Gap4).
Adds 2 nullable JSONB columns to ai.user_taste_profile, defaulting to '{}'
so the explicit-column SELECT in PostgresTasteProfileStore never hard-fails
on a pre-migration row. Idempotent under re-run via IF NOT EXISTS.

Deploy order: migration-first, then code (the explicit-column SELECT in
`taste_profile_pg._aget_or_create` requires these columns to exist).

@MX:SPEC: SPEC-AGENT-V3-REACT
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET search_path TO ai")
    op.execute(
        """
        ALTER TABLE ai.user_taste_profile
            ADD COLUMN IF NOT EXISTS disliked_brands_ts   JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN IF NOT EXISTS disliked_keywords_ts JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai.user_taste_profile
            DROP COLUMN IF EXISTS disliked_keywords_ts,
            DROP COLUMN IF EXISTS disliked_brands_ts
        """
    )
