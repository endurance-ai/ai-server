"""add conditional personalized curation sections

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM ai.curation_candidates WHERE section_id = 'popular'")
    op.execute("DELETE FROM ai.curation_sections WHERE section_id = 'popular'")
    op.execute(
        """
        UPDATE ai.curation_sections
        SET sort_order = CASE section_id
                WHEN 'trending-search' THEN 1
                WHEN 'under-100' THEN 2
            END,
            updated_at = now()
        WHERE section_id IN ('trending-search', 'under-100')
        """
    )
    op.execute(
        """
        INSERT INTO ai.curation_sections
            (section_id, gender, slot_type, display_type, title, subtitle,
             product_ids, sort_order, is_active, updated_at)
        SELECT section_id, gender, 'auto', 'default', title, NULL,
               '{}'::bigint[], sort_order, true, now()
        FROM (
            VALUES
                ('recently-viewed', '최근 본 상품', 3),
                ('saved-on-sale', '방금 할인된 찜', 4),
                ('taste-picks', '취향 저격 아이템', 5),
                ('favorite-brands', '내가 좋아하는 브랜드', 6)
        ) AS section(section_id, title, sort_order)
        CROSS JOIN (VALUES ('women'), ('men')) AS audience(gender)
        ON CONFLICT (section_id, gender) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM ai.curation_sections
        WHERE section_id IN ('recently-viewed', 'saved-on-sale', 'taste-picks', 'favorite-brands')
        """
    )
    op.execute(
        """
        UPDATE ai.curation_sections
        SET sort_order = CASE section_id
                WHEN 'trending-search' THEN 2
                WHEN 'under-100' THEN 3
            END,
            updated_at = now()
        WHERE section_id IN ('trending-search', 'under-100')
        """
    )
    op.execute(
        """
        INSERT INTO ai.curation_sections
            (section_id, gender, slot_type, display_type, title, subtitle,
             product_ids, sort_order, is_active, updated_at)
        SELECT 'popular', gender, 'auto', 'default', '지금 인기',
               '이번 주 가장 많이 담긴', '{}'::bigint[], 1, true, now()
        FROM (VALUES ('women'), ('men')) AS audience(gender)
        ON CONFLICT (section_id, gender) DO NOTHING
        """
    )
