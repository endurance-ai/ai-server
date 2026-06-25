"""add ai.product_views — user product view history

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-25
"""

from alembic import op

revision = "0011"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai.product_views (
            view_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id          UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            product_id       BIGINT NOT NULL,
            session_id       UUID NOT NULL,
            source_search_id UUID,
            dwell_ms         INTEGER,
            viewed_at        TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_product_views_user_session
            ON ai.product_views(user_id, session_id, viewed_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_product_views_user_product
            ON ai.product_views(user_id, product_id, viewed_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.product_views")
