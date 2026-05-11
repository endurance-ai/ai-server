"""create memory tables (user_taste_profile, user_session)

Revision ID: 0001
Revises:
Create Date: 2026-05-11

SPEC-MEMORY-001 REQ-MEMORY-MIGRATION-001 baseline revision.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # @MX:NOTE: All memory tables live in dedicated `ai` schema, isolated from
    # product catalog (`public.*`). The schema itself is created as a one-time
    # bootstrap by a superuser — this migration does NOT create the schema
    # (ai_user lacks DB-level CREATE privilege on dev-app, by design).
    # Test environments bootstrap the schema in conftest before invoking alembic.
    op.execute("SET search_path TO ai")
    # user_taste_profile
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.user_taste_profile (
            user_key            text PRIMARY KEY,
            liked_brands        jsonb NOT NULL DEFAULT '{}'::jsonb,
            disliked_brands     jsonb NOT NULL DEFAULT '{}'::jsonb,
            liked_keywords      jsonb NOT NULL DEFAULT '{}'::jsonb,
            disliked_keywords   jsonb NOT NULL DEFAULT '{}'::jsonb,
            price_min_observed  integer,
            price_max_observed  integer,
            last_active         timestamptz(6) NOT NULL DEFAULT now(),
            updated_at          timestamptz(6) NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_taste_last_active ON ai.user_taste_profile (last_active)")

    # user_session
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.user_session (
            chat_id                            bigint PRIMARY KEY,
            state                              text NOT NULL DEFAULT 'idle',
            from_user_id                       bigint,
            image_url                          text,
            detected_items                     jsonb NOT NULL DEFAULT '[]'::jsonb,
            selected_item_index                integer,
            vision_keywords                    jsonb NOT NULL DEFAULT '[]'::jsonb,
            vision_item                        text,
            vision_result                      jsonb,
            vision_selected_item_index         integer,
            vision_outfit_style_node_primary   text,
            vision_outfit_style_node_secondary text,
            vision_outfit_mood_tags            jsonb NOT NULL DEFAULT '[]'::jsonb,
            vision_outfit_gender               text,
            user_intent                        text,
            last_results                       jsonb NOT NULL DEFAULT '[]'::jsonb,
            shown_product_ids                  jsonb NOT NULL DEFAULT '[]'::jsonb,
            last_critique_summary              text,
            boost_keywords                     jsonb NOT NULL DEFAULT '[]'::jsonb,
            clarify_axis                       text,
            clarify_value                      text,
            lang                               text NOT NULL DEFAULT 'en',
            last_active                        timestamptz(6) NOT NULL DEFAULT now(),
            ttl_expires_at                     timestamptz(6) NOT NULL DEFAULT (now() + interval '1800 seconds')
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_session_ttl ON ai.user_session (ttl_expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.user_session")
    op.execute("DROP TABLE IF EXISTS ai.user_taste_profile")
