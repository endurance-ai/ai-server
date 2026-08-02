"""add notification tables — baseline snapshot, notification log, job watermark

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-01

알림 1차(재입고 / 가격 하락 / 관심 브랜드 신규 상품)의 저장 계층.

- ai.saved_product_baseline: 찜한 상품의 기준가·기준 재고 스냅샷. 찜 생성 시 기록하고
  알림 발송 후 현재값으로 갱신한다. "직전 크롤 대비" 가 아니라 "네가 찜한 뒤" 를
  기준으로 삼아야 하루 3%씩 5일 내리는 하락을 놓치지 않는다.
- ai.notifications: 피드 이력 + 중복 방지 + 캡 집계의 단일 출처.
  (user_id, kind, product_id, created_on) 유니크로 일 단위 멱등 — 같은 배치를
  두 번 돌려도 알림이 늘지 않는다. created_on 은 KST 날짜(배치가 KST 기준 하루
  1회 도는 것과 맞춘다). product_id 가 NULL 인 종류를 대비해 coalesce 로 감싼다.
- ai.notification_job_state: 배치 마지막 실행 시각. 감지 기준점이 아니다 — 신규 상품은
  워터마크가 아니라 보존창 + ai.notifications 안티조인으로 잡는다(성별이 VLM 배치로
  나중에 붙기 때문. app/services/notifications.py `_NEW_PRODUCT_SQL` 주석 참조).
  타이머로 도는 배치라 이 테이블이 "살아 있는지" 확인하는 유일한 지점이다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.saved_product_baseline (
            user_id           UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            product_id        BIGINT NOT NULL,
            baseline_price    NUMERIC,
            baseline_in_stock BOOLEAN,
            baseline_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, product_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.notifications (
            id            BIGSERIAL PRIMARY KEY,
            user_id       UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            kind          TEXT NOT NULL CHECK (kind IN ('restock', 'price_drop', 'brand_new_product')),
            product_id    BIGINT,
            brand_node_id BIGINT,
            payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_on    DATE NOT NULL DEFAULT (now() AT TIME ZONE 'Asia/Seoul')::date,
            sent_at       TIMESTAMPTZ,
            read_at       TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_notifications_daily
        ON ai.notifications (user_id, kind, coalesce(product_id, -1), created_on)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notifications_user_feed
        ON ai.notifications (user_id, created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.notification_job_state (
            job        TEXT PRIMARY KEY,
            watermark  TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.notification_job_state")
    op.execute("DROP TABLE IF EXISTS ai.notifications")
    op.execute("DROP TABLE IF EXISTS ai.saved_product_baseline")
