"""allow 'guest' tier on ai.user_profiles

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-24

Web landing (ad-driven, no login) proxies all traffic through a single
guest service account so cost is capped by one site-wide daily token total
(app/core/config.py CAP_TIER_GUEST, app/infrastructure/cache/token_cap.py).
That account's `ai.user_profiles.tier` needs to be set to 'guest', but the
CHECK constraint from migration 0009 (extended by 0017 for 'developer')
only allows ('free','basic','pro','premium','developer'). Extend it to
include 'guest' so the post-deploy `UPDATE ... SET tier = 'guest'` on the
guest service account doesn't fail with user_profiles_tier_check.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai.user_profiles DROP CONSTRAINT IF EXISTS user_profiles_tier_check")
    op.execute(
        """
        ALTER TABLE ai.user_profiles
            ADD CONSTRAINT user_profiles_tier_check
            CHECK (tier IN ('free', 'basic', 'pro', 'premium', 'developer', 'guest'))
        """
    )


def downgrade() -> None:
    # Reverting requires no row to hold 'guest'; demote any to 'free' first.
    op.execute("UPDATE ai.user_profiles SET tier = 'free' WHERE tier = 'guest'")
    op.execute("ALTER TABLE ai.user_profiles DROP CONSTRAINT IF EXISTS user_profiles_tier_check")
    op.execute(
        """
        ALTER TABLE ai.user_profiles
            ADD CONSTRAINT user_profiles_tier_check
            CHECK (tier IN ('free', 'basic', 'pro', 'premium', 'developer'))
        """
    )
