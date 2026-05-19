"""add langfuse_trace to card_impression

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-19

P0 user-feedback scores — binds the ORIGINAL recommendation Langfuse trace id
to each impression row so click / no_click can be retro-scored against the
trace that produced the cards (NOT the later click webhook's trace). Nullable
+ idempotent — old rows simply remain unscored (acceptable: only in-flight
impressions at deploy time are affected).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai.card_impression ADD COLUMN IF NOT EXISTS langfuse_trace text")


def downgrade() -> None:
    op.execute("ALTER TABLE ai.card_impression DROP COLUMN IF EXISTS langfuse_trace")
