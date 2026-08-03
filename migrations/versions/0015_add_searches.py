"""add ai.searches — 검색 1회(결과셋) 영속화

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-26

리스트/히스토리 면(LS_01)의 토대. chat 검색이 결과셋을 영속화해 [더보기]·결과셋
히스토리·feedback.search_id·product_views.source_search_id 가 참조할 수 있게 한다.
is_listed=false 로 시작 → [더보기](Get Result Set Page 첫 호출) 시 true 승급.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.searches (
            search_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id        UUID NOT NULL REFERENCES ai.chat_sessions(session_id) ON DELETE CASCADE,
            user_id           UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            title             TEXT,
            scope             TEXT NOT NULL DEFAULT 'session',
            trigger           TEXT,
            anchor_product_id BIGINT,
            product_ids       BIGINT[] NOT NULL DEFAULT '{}',
            cover_image_urls  TEXT[] NOT NULL DEFAULT '{}',
            total             INT NOT NULL DEFAULT 0,
            is_listed         BOOLEAN NOT NULL DEFAULT false,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_searches_session ON ai.searches(session_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_searches_user ON ai.searches(user_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.searches")
