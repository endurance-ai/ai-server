"""wire brand_sale into the durable APNs outbox — new message category

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-06

brand_sale 는 0025 에서 감지·상태(ai.brand_sale_state)·알림함(ai.notifications) 까지만
붙었고, 실제 APNs 푸시 파이프라인(ai.notification_messages → ai.notification_deliveries)
을 타지 못했다. 아웃박스가 브랜드 세일을 다이제스트로 묶어 발송하려면 메시지 카테고리
CHECK 가 새 값 'brand_sale_digest' 를 허용해야 한다.

- ai.notification_messages.category CHECK 확장: 'brand_sale_digest' 추가. 0024 의 인라인
  CHECK (category IN (...)) 는 이름 없이 정의돼 Postgres 가 <table>_<col>_check 규칙으로
  notification_messages_category_check 를 부여한다. IF EXISTS 로 안전하게 떼고 확장판을 단다.

brand_sale 을 brand_new(brand_new_digest)와 다른 카테고리로 분리하는 이유: 주간 캡
집계(fetch_brand_new_days_last_week)가 category = 'brand_new_digest' 로 세므로, 같은
카테고리를 공유하면 brand_new 의 주 N일 캡에 brand_sale 발송이 섞여 카운트가 오염된다.
브랜드 세일은 캡/전환 게이트가 별도라 전용 카테고리를 갖는다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai.notification_messages DROP CONSTRAINT IF EXISTS notification_messages_category_check")
    op.execute(
        """
        ALTER TABLE ai.notification_messages
            ADD CONSTRAINT notification_messages_category_check
            CHECK (category IN ('saved_product_digest', 'brand_new_digest', 'brand_sale_digest'))
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ai.notification_messages DROP CONSTRAINT IF EXISTS notification_messages_category_check")
    op.execute(
        """
        ALTER TABLE ai.notification_messages
            ADD CONSTRAINT notification_messages_category_check
            CHECK (category IN ('saved_product_digest', 'brand_new_digest'))
        """
    )
