"""add ai.uploads — direct image upload reservations

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-28

POST /v1/uploads records S3/CloudFront upload metadata before returning a
presigned PUT URL to the client.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.uploads (
            upload_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id      UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            object_key   TEXT NOT NULL UNIQUE,
            filename     TEXT NOT NULL,
            content_type TEXT NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
            size_bytes   INT NOT NULL CHECK (size_bytes > 0),
            image_url    TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'uploaded', 'failed', 'expired')),
            expires_at   TIMESTAMPTZ NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_uploads_user_created ON ai.uploads(user_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_uploads_status_expires ON ai.uploads(status, expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.uploads")
