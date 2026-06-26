"""add misc tables — feedback, devices, legal_consents, notification_settings

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-25
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Feedback submissions
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai.feedbacks (
            feedback_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            search_id    UUID,
            rating       TEXT NOT NULL CHECK (rating IN ('positive', 'negative')),
            reasons      TEXT[] NOT NULL DEFAULT '{}',
            comment      TEXT NOT NULL,
            consent      BOOLEAN NOT NULL DEFAULT FALSE,
            created_at   TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_feedbacks_user ON ai.feedbacks(user_id, created_at DESC)")

    # APNs device token registry
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai.devices (
            device_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            apns_token   TEXT NOT NULL,
            platform     TEXT NOT NULL DEFAULT 'ios',
            app_version  TEXT,
            device_model TEXT,
            registered_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (user_id, apns_token)
        )
    """)

    # Notification preferences on user profile (JSONB column)
    op.execute("""
        ALTER TABLE ai.user_profiles
        ADD COLUMN IF NOT EXISTS notification_settings JSONB
            DEFAULT '{"release_alerts": true, "taste_push": true, "system": true}'::jsonb
    """)

    # Legal consent records
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai.legal_consents (
            consent_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id       UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            document_type TEXT NOT NULL CHECK (document_type IN ('tos', 'privacy')),
            version       TEXT NOT NULL,
            consented_at  TIMESTAMPTZ DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_legal_consents_user
            ON ai.legal_consents(user_id, document_type, consented_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.legal_consents")
    op.execute("ALTER TABLE ai.user_profiles DROP COLUMN IF EXISTS notification_settings")
    op.execute("DROP TABLE IF EXISTS ai.devices")
    op.execute("DROP TABLE IF EXISTS ai.feedbacks")
