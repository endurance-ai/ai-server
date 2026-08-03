"""add onboarded_at + onboarding state + pinterest cache columns to user_session

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-15

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-MIGRATION-001 + REQ-ONBOARD-PINTEREST-007.
Adds 7 nullable columns to ai.user_session. Backfills existing rows with
onboarded_at = now() (treated as already-onboarded — REQ-ONBOARD-MIGRATION-001).
Idempotent under re-run via IF NOT EXISTS.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET search_path TO ai")
    op.execute(
        """
        ALTER TABLE ai.user_session
            ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS onboard_stage TEXT NULL,
            ADD COLUMN IF NOT EXISTS onboard_selections JSONB NULL,
            ADD COLUMN IF NOT EXISTS onboard_card_message_id BIGINT NULL,
            ADD COLUMN IF NOT EXISTS last_pinterest_scrape_url TEXT NULL,
            ADD COLUMN IF NOT EXISTS last_pinterest_scrape_at TIMESTAMPTZ NULL,
            ADD COLUMN IF NOT EXISTS last_pinterest_pins JSONB NULL
        """
    )
    # REQ-ONBOARD-MIGRATION-001 backfill — existing users = already-onboarded.
    op.execute("UPDATE ai.user_session SET onboarded_at = now() WHERE onboarded_at IS NULL")


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai.user_session
            DROP COLUMN IF EXISTS last_pinterest_pins,
            DROP COLUMN IF EXISTS last_pinterest_scrape_at,
            DROP COLUMN IF EXISTS last_pinterest_scrape_url,
            DROP COLUMN IF EXISTS onboard_card_message_id,
            DROP COLUMN IF EXISTS onboard_selections,
            DROP COLUMN IF EXISTS onboard_stage,
            DROP COLUMN IF EXISTS onboarded_at
        """
    )
