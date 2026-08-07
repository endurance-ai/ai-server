"""notification inbox feed + brand follow + brand-sale detection state

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-06

알림함(피드) · 브랜드 팔로우 · 브랜드 세일 감지의 저장 계층 확장이다. 신규 시스템이
아니라 이미 있는 세 테이블을 요청 계약에 맞게 늘린다.

- ai.user_brand_picks.notify_enabled: 온보딩 브랜드 픽 테이블을 팔로우 저장소로 그대로
  재사용한다. 팔로우 시 기본 on. 유저가 브랜드 알림만 끄면 false 로 남고, brand_sale /
  brand_new 팔로워 조회는 이 컬럼으로 게이트한다.
- ai.notifications.kind CHECK 확장: 'brand_sale' 추가. 0023 의 인라인
  CHECK (kind IN (...)) 는 Postgres 가 notifications_kind_check 로 자동 명명하므로
  이름으로 DROP 후 재생성한다.
- ai.brand_sale_state: 브랜드별 "세일 중" 상태(on_sale) + 마지막 세일 비율(ratio) 추적.
  재입고 판정(baseline_in_stock false→true)과 같은 전환 감지 패턴 — 비율이 임계값을
  처음 넘는 순간에만 알림하고, 연속 세일 기간 내내 재발송하지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai.user_brand_picks ADD COLUMN IF NOT EXISTS notify_enabled BOOLEAN NOT NULL DEFAULT true")

    # 0023 의 인라인 CHECK 는 이름 없이 정의돼 Postgres 가 <table>_<col>_check 규칙으로
    # notifications_kind_check 를 부여한다. IF EXISTS 로 안전하게 떼고 확장판을 단다.
    op.execute("ALTER TABLE ai.notifications DROP CONSTRAINT IF EXISTS notifications_kind_check")
    op.execute(
        """
        ALTER TABLE ai.notifications
            ADD CONSTRAINT notifications_kind_check
            CHECK (kind IN ('restock', 'price_drop', 'brand_new_product', 'brand_sale'))
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.brand_sale_state (
            brand_node_id BIGINT PRIMARY KEY,
            on_sale       BOOLEAN NOT NULL DEFAULT false,
            ratio         NUMERIC,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.brand_sale_state")
    op.execute("ALTER TABLE ai.notifications DROP CONSTRAINT IF EXISTS notifications_kind_check")
    op.execute(
        """
        ALTER TABLE ai.notifications
            ADD CONSTRAINT notifications_kind_check
            CHECK (kind IN ('restock', 'price_drop', 'brand_new_product'))
        """
    )
    op.execute("ALTER TABLE ai.user_brand_picks DROP COLUMN IF EXISTS notify_enabled")
