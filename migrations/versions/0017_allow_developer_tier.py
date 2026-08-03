"""allow 'developer' tier on ai.user_profiles

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-30

The consumer-app cap path (`chat_service._APP_TO_CAP_TIER`) already maps
`developer → developer` (unlimited, cap 0), but the ai.user_profiles tier
CHECK constraint from migration 0009 only allowed ('free','basic','pro',
'premium'). That gap made `UPDATE ai.user_profiles SET tier='developer'`
fail with user_profiles_tier_check. Extend the constraint to include
'developer' so internal developers can be granted the unlimited cap tier.

The original constraint was inline/anonymous in CREATE TABLE, so Postgres
auto-named it `user_profiles_tier_check`. We drop that and re-add a named
constraint with the extended value set.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai.user_profiles DROP CONSTRAINT IF EXISTS user_profiles_tier_check")
    op.execute(
        """
        ALTER TABLE ai.user_profiles
            ADD CONSTRAINT user_profiles_tier_check
            CHECK (tier IN ('free', 'basic', 'pro', 'premium', 'developer'))
        """
    )


def downgrade() -> None:
    # Reverting requires no row to hold 'developer'; demote any to 'premium' first.
    op.execute("UPDATE ai.user_profiles SET tier = 'premium' WHERE tier = 'developer'")
    op.execute("ALTER TABLE ai.user_profiles DROP CONSTRAINT IF EXISTS user_profiles_tier_check")
    op.execute(
        """
        ALTER TABLE ai.user_profiles
            ADD CONSTRAINT user_profiles_tier_check
            CHECK (tier IN ('free', 'basic', 'pro', 'premium'))
        """
    )
