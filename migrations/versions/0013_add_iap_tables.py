"""add iap_transactions table

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-26
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai.iap_transactions (
            txn_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id              UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            apple_txn_id         TEXT NOT NULL UNIQUE,
            apple_original_txn_id TEXT NOT NULL,
            product_id           TEXT NOT NULL,
            tier                 TEXT NOT NULL,
            purchase_date        TIMESTAMPTZ NOT NULL,
            expires_date         TIMESTAMPTZ,
            environment          TEXT NOT NULL DEFAULT 'Production'
                                     CHECK (environment IN ('Sandbox', 'Production')),
            notification_type    TEXT,
            raw_payload          JSONB,
            created_at           TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_iap_txn_user
            ON ai.iap_transactions(user_id, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_iap_txn_original
            ON ai.iap_transactions(apple_original_txn_id, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.iap_transactions")
