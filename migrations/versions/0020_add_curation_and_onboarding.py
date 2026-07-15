"""add curation sections cache + onboarding brand picks

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-15

- ai.curation_sections: server-driven 메인 구좌 캐시. 백그라운드 refresher가
  auto 구좌(인기 브랜드/트렌딩 검색/Under $100)를 계산해 upsert하고, 노션
  "큐레이션 구좌" DB 동기화가 editorial 구좌를 upsert한다. GET /v1/curation은
  이 테이블만 읽는다 (요청 경로에서 집계 없음).
- ai.user_brand_picks: 온보딩 ④ 브랜드 픽 — {스타일 노드, 브랜드} 쌍 저장.
  brand_id 가 원신호, style_node_id 는 저장 시점의 brand_nodes.primary_style_node_id
  스냅샷 (수기 지정 그리드 픽 / 직접 검색 추가 모두 동일 경로).
- ai.user_profiles.onboarded_at: 앱 온보딩 완료 시각. 재로그인 시 재확인 여부
  판단용 (텔레그램 쪽 ai.user_session.onboarded_at 와는 별개 채널).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.curation_sections (
            section_id  TEXT NOT NULL,
            gender      TEXT NOT NULL CHECK (gender IN ('women', 'men')),
            slot_type   TEXT NOT NULL CHECK (slot_type IN ('auto', 'editorial')),
            title       TEXT NOT NULL,
            subtitle    TEXT,
            product_ids BIGINT[] NOT NULL DEFAULT '{}',
            sort_order  INT NOT NULL DEFAULT 0,
            is_active   BOOLEAN NOT NULL DEFAULT true,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (section_id, gender)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.user_brand_picks (
            user_id       UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            brand_id      BIGINT NOT NULL,
            style_node_id BIGINT,
            source        TEXT NOT NULL DEFAULT 'onboarding',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, brand_id)
        )
        """
    )
    op.execute("ALTER TABLE ai.user_profiles ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE ai.user_profiles DROP COLUMN IF EXISTS onboarded_at")
    op.execute("DROP TABLE IF EXISTS ai.user_brand_picks")
    op.execute("DROP TABLE IF EXISTS ai.curation_sections")
