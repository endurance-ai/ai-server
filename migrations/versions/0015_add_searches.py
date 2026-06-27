"""add ai.searches — persisted search result sets (result-set history)

Backs GET /v1/results, GET /v1/results/{search_id}, GET /v1/history.
One row per search turn; product_ids holds the ranked result set (cosine order).
`is_listed` flips TRUE when the client opens the result set as a list view
(first GET /v1/results/{search_id}); /v1/results lists only is_listed rows.

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-27
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai.searches (
            search_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id   UUID NOT NULL REFERENCES ai.chat_sessions(session_id) ON DELETE CASCADE,
            user_id      UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            query_text   TEXT NOT NULL,
            product_ids  BIGINT[] NOT NULL DEFAULT '{}',
            result_count INTEGER NOT NULL DEFAULT 0,
            is_listed    BOOLEAN NOT NULL DEFAULT FALSE,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_searches_session_created
            ON ai.searches(session_id, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.searches")
