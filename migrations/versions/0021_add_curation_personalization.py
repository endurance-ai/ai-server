"""add curation candidates and style-score personalization

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai.curation_sections
            ADD COLUMN IF NOT EXISTS source_page_id TEXT,
            ADD COLUMN IF NOT EXISTS products_updated_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.curation_candidates (
            section_id     TEXT NOT NULL,
            gender         TEXT NOT NULL CHECK (gender IN ('women', 'men')),
            product_id     BIGINT NOT NULL,
            is_hot         BOOLEAN NOT NULL DEFAULT false,
            base_score     DOUBLE PRECISION NOT NULL DEFAULT 0,
            base_rank      INT NOT NULL,
            brand_key      TEXT NOT NULL,
            brand_node_id  BIGINT,
            style_node_id  BIGINT,
            generated_for  DATE NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (section_id, gender, product_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_curation_candidates_lookup
        ON ai.curation_candidates (gender, section_id, generated_for, is_hot, base_rank)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.user_style_scores (
            user_id       UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            style_node_id BIGINT NOT NULL,
            score         DOUBLE PRECISION NOT NULL DEFAULT 0,
            signal_count  INT NOT NULL DEFAULT 0,
            last_event_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, style_node_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.taste_signal_events (
            event_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            dedupe_key    TEXT NOT NULL UNIQUE,
            user_id       UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            style_node_id BIGINT NOT NULL,
            signal_type   TEXT NOT NULL,
            weight        DOUBLE PRECISION NOT NULL,
            metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
            occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_taste_signal_events_user_time
        ON ai.taste_signal_events (user_id, occurred_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.curation_impressions (
            impression_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id               UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            section_id            TEXT NOT NULL,
            product_id            BIGINT NOT NULL,
            style_node_id         BIGINT,
            position              INT,
            shown_on              DATE NOT NULL DEFAULT CURRENT_DATE,
            shown_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            interacted_at         TIMESTAMPTZ,
            negative_attributed_at TIMESTAMPTZ,
            UNIQUE (user_id, section_id, product_id, shown_on)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_curation_impressions_attribution
        ON ai.curation_impressions
            (user_id, style_node_id, shown_at)
        WHERE interacted_at IS NULL AND negative_attributed_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.curation_impressions")
    op.execute("DROP TABLE IF EXISTS ai.taste_signal_events")
    op.execute("DROP TABLE IF EXISTS ai.user_style_scores")
    op.execute("DROP TABLE IF EXISTS ai.curation_candidates")
    op.execute(
        """
        ALTER TABLE ai.curation_sections
            DROP COLUMN IF EXISTS products_updated_at,
            DROP COLUMN IF EXISTS source_page_id
        """
    )
