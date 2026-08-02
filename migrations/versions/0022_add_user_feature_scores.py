"""add per-user feature-axis taste scores (color/fit/material/pattern/neckline)

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-29

Extends the style-node taste engine (0021) to the visual-feature axes unlocked by
the Product Enrichment pipeline (public.product_features.feature_metadata). Every
product-level signal (save/unsave/view/outbound) now also teaches the user's color/
fit/material/pattern/neckline preference, not just the brand's style node.

- ai.user_feature_scores: decayed preference score per (user, axis, value). Mirrors
  ai.user_style_scores math exactly (half-life 30d, clamp [-20, 20], upsert on signal).
- ai.taste_feature_events: idempotent per-(signal, axis, value) event log. Separate
  from ai.taste_signal_events because that table requires style_node_id and has no
  axis/value columns.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.user_feature_scores (
            user_id       UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            axis          TEXT NOT NULL,
            value         TEXT NOT NULL,
            score         DOUBLE PRECISION NOT NULL DEFAULT 0,
            signal_count  INT NOT NULL DEFAULT 0,
            last_event_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, axis, value)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_feature_scores_axis
        ON ai.user_feature_scores (user_id, axis, score DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.taste_feature_events (
            event_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            dedupe_key  TEXT NOT NULL UNIQUE,
            user_id     UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            axis        TEXT NOT NULL,
            value       TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            weight      DOUBLE PRECISION NOT NULL,
            product_id  BIGINT,
            occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_taste_feature_events_user_time
        ON ai.taste_feature_events (user_id, occurred_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.taste_feature_events")
    op.execute("DROP TABLE IF EXISTS ai.user_feature_scores")
