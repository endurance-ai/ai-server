"""노션 이관 정리 — ai.curation_sections.source_page_id 제거

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-13

`source_page_id` 는 노션 페이지 id 를 담던 컬럼이다(0021). 구좌 관리가 어드민
페이지로 옮겨가면서 이 값을 쓰는 코드가 없어졌다 — 유일한 writer 였던
`sync_notion_sections` 를 함께 제거했다. 이름부터 노션에 묶여 있어 남겨두면
어드민 구현 때 오해를 부른다.

downgrade 는 컬럼을 되살리지만 값은 복구하지 않는다. 노션 경로가 이미 제거돼
되살릴 writer 도 없다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai.curation_sections DROP COLUMN IF EXISTS source_page_id")


def downgrade() -> None:
    op.execute("ALTER TABLE ai.curation_sections ADD COLUMN IF NOT EXISTS source_page_id TEXT")
