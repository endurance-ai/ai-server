"""persist the app-chat edit-shop search scope

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai.user_session ADD COLUMN IF NOT EXISTS request_platform TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE ai.user_session DROP COLUMN IF EXISTS request_platform")
