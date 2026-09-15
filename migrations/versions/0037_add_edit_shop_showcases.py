"""add edit-shop rankings and curation destinations

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | Sequence[str] | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.edit_shop_rankings (
            platform       TEXT NOT NULL,
            gender         TEXT NOT NULL CHECK (gender IN ('women', 'men')),
            category       TEXT NOT NULL,
            product_id     BIGINT NOT NULL,
            base_score     DOUBLE PRECISION NOT NULL DEFAULT 0,
            final_rank     INTEGER NOT NULL CHECK (final_rank > 0),
            ranking_source TEXT NOT NULL CHECK (
                ranking_source IN ('what100', 'popular', 'daily_shuffle', 'what100_plus_shuffle')
            ),
            generated_for  DATE NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (generated_for, platform, gender, category, product_id),
            UNIQUE (generated_for, platform, gender, category, final_rank)
        )
        """
    )
    # Production shares the crawler-owned public schema, while some hermetic
    # AI-only migration tests intentionally bootstrap no public.products table.
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('public.products') IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM pg_constraint
               WHERE conname = 'edit_shop_rankings_product_id_fkey'
                 AND conrelid = 'ai.edit_shop_rankings'::regclass
             ) THEN
            ALTER TABLE ai.edit_shop_rankings
              ADD CONSTRAINT edit_shop_rankings_product_id_fkey
              FOREIGN KEY (product_id) REFERENCES public.products(id) ON DELETE CASCADE;
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_edit_shop_rankings_lookup
        ON ai.edit_shop_rankings
            (platform, gender, category, generated_for DESC, final_rank)
        """
    )
    op.execute(
        """
        ALTER TABLE ai.curation_sections
          ADD COLUMN IF NOT EXISTS destination_type TEXT,
          ADD COLUMN IF NOT EXISTS destination_key TEXT
        """
    )
    op.execute(
        """
        ALTER TABLE ai.curation_sections
          DROP CONSTRAINT IF EXISTS curation_sections_destination_check
        """
    )
    op.execute(
        """
        ALTER TABLE ai.curation_sections
          ADD CONSTRAINT curation_sections_destination_check CHECK (
            (destination_type IS NULL AND destination_key IS NULL)
            OR (destination_type = 'edit_shop' AND btrim(destination_key) <> '')
          )
        """
    )

    # Replace the three seasonal trending showcases with five edit-shop
    # destinations. Product imagery remains owned by the mobile client.
    op.execute(
        """
        UPDATE ai.curation_sections
        SET is_active = false, updated_at = now()
        WHERE display_type = 'trending'
          AND section_id NOT LIKE 'edit-shop-%'
        """
    )
    op.execute(
        """
        INSERT INTO ai.curation_sections
            (section_id, gender, slot_type, display_type, title, subtitle,
             product_ids, sort_order, is_active, destination_type,
             destination_key, updated_at)
        VALUES
          ('edit-shop-slowsteadyclub', 'women', 'editorial', 'trending',
           'SLOW STEADY CLUB', NULL, '{}', 10, true, 'edit_shop', 'slowsteadyclub', now()),
          ('edit-shop-slowsteadyclub', 'men', 'editorial', 'trending',
           'SLOW STEADY CLUB', NULL, '{}', 10, true, 'edit_shop', 'slowsteadyclub', now()),
          ('edit-shop-8division', 'women', 'editorial', 'trending',
           '8DIVISION', NULL, '{}', 11, true, 'edit_shop', '8division', now()),
          ('edit-shop-8division', 'men', 'editorial', 'trending',
           '8DIVISION', NULL, '{}', 11, true, 'edit_shop', '8division', now()),
          ('edit-shop-etcseoul', 'women', 'editorial', 'trending',
           'ETC Seoul', NULL, '{}', 12, true, 'edit_shop', 'etcseoul', now()),
          ('edit-shop-etcseoul', 'men', 'editorial', 'trending',
           'ETC Seoul', NULL, '{}', 12, true, 'edit_shop', 'etcseoul', now()),
          ('edit-shop-fr8ight', 'women', 'editorial', 'trending',
           'FR8IGHT', NULL, '{}', 13, true, 'edit_shop', 'fr8ight', now()),
          ('edit-shop-fr8ight', 'men', 'editorial', 'trending',
           'FR8IGHT', NULL, '{}', 13, true, 'edit_shop', 'fr8ight', now()),
          ('edit-shop-kith', 'women', 'editorial', 'trending',
           'KITH', NULL, '{}', 14, true, 'edit_shop', 'kith', now()),
          ('edit-shop-kith', 'men', 'editorial', 'trending',
           'KITH', NULL, '{}', 14, true, 'edit_shop', 'kith', now())
        ON CONFLICT (section_id, gender) DO UPDATE SET
            slot_type = EXCLUDED.slot_type,
            display_type = EXCLUDED.display_type,
            title = EXCLUDED.title,
            subtitle = EXCLUDED.subtitle,
            sort_order = EXCLUDED.sort_order,
            is_active = EXCLUDED.is_active,
            destination_type = EXCLUDED.destination_type,
            destination_key = EXCLUDED.destination_key,
            updated_at = now()
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM ai.curation_sections WHERE section_id LIKE 'edit-shop-%'")
    op.execute("ALTER TABLE ai.curation_sections DROP CONSTRAINT IF EXISTS curation_sections_destination_check")
    op.execute(
        """
        ALTER TABLE ai.curation_sections
          DROP COLUMN IF EXISTS destination_key,
          DROP COLUMN IF EXISTS destination_type
        """
    )
    op.execute("DROP TABLE IF EXISTS ai.edit_shop_rankings")
