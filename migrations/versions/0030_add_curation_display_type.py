"""큐레이션 구좌 표시 타입 — ai.curation_sections.display_type

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-13

앱이 트렌딩 구좌를 전용 디자인으로 그린다. 기존 `slot_type` 은 **데이터 출처**
(auto = 리프레셔가 계산 / editorial = 사람이 고른 목록)를 뜻하는 값이라 렌더러
분기에 재사용하면 의미가 섞인다 — 실제로 트렌딩 3개 구좌가 전부 editorial 이라
slot_type 만으로는 구분이 불가능하다. 표시용 축을 따로 둔다.

server-driven 원칙 유지: 어떤 구좌를 어떤 디자인으로 그릴지는 이 컬럼이 정하고
앱은 값에 따라 렌더러만 고른다. 구좌 디자인 변경에 앱 배포가 필요 없다.

값은 최소로 시작한다. 'default' 는 기존 그리드 — 컬럼을 추가해도 기존 구좌는
전부 default 라 앱 동작이 그대로다. 새 디자인이 생기면 CHECK 에 값을 더한다.

노션 파서(`sync_notion_sections`)는 이 컬럼을 쓰지 않는다. INSERT 목록에 없어
신규 행은 기본값을 받고, ON CONFLICT DO UPDATE 도 이 컬럼을 건드리지 않아
시드가 넣은 값이 노션 동기화에 덮이지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ai.curation_sections
            ADD COLUMN IF NOT EXISTS display_type TEXT NOT NULL DEFAULT 'default'
        """
    )
    op.execute("ALTER TABLE ai.curation_sections DROP CONSTRAINT IF EXISTS curation_sections_display_type_check")
    op.execute(
        """
        ALTER TABLE ai.curation_sections
            ADD CONSTRAINT curation_sections_display_type_check
            CHECK (display_type IN ('default', 'trending'))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ai.curation_sections DROP CONSTRAINT IF EXISTS curation_sections_display_type_check")
    op.execute("ALTER TABLE ai.curation_sections DROP COLUMN IF EXISTS display_type")
