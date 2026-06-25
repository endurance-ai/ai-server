"""add ai.saves — user product bookmarks

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-25
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai.saves (
            save_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id    UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            product_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (user_id, product_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_saves_user_id ON ai.saves(user_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.saves")
