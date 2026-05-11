"""create card_impression table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-11

SPEC-IMPLICIT-FB-001 / REQ-FB-MIGRATION-001 — adds `ai.card_impression` with
4 indexes (1 partial). Idempotent under re-run.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET search_path TO ai")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.card_impression (
            id                    bigserial PRIMARY KEY,
            chat_id               bigint NOT NULL,
            from_user_id          bigint,
            product_id            text NOT NULL,
            brand                 text NOT NULL DEFAULT '',
            keywords              jsonb NOT NULL DEFAULT '[]'::jsonb,
            shown_at              timestamptz NOT NULL DEFAULT now(),
            attribution_window_s  integer NOT NULL,
            click_status          text,
            click_at              timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_card_impression_chat_pending "
        "ON ai.card_impression (chat_id) WHERE click_status IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_card_impression_chat_product ON ai.card_impression (chat_id, product_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_card_impression_cleanup "
        "ON ai.card_impression (click_at, shown_at) WHERE click_status IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.card_impression")
