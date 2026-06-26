"""add ai.subscriptions + ai.iap_events — subscription lifecycle + webhook idempotency

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-26

명세 정합: tier-only 모델 → subscription 생애주기 모델.
- ai.subscriptions  : 구독 1건의 현재 상태 (original_transaction_id 기준 upsert)
                       status(active/grace/expired/revoked) + auto_renew + expires_at
- ai.iap_events     : Apple S2S 알림 멱등성 (notification_uuid UNIQUE)

user_profiles.tier 는 파생 캐시로 계속 동기화 (검색 gender gate / daily cap 등 기존 소비자 유지).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.subscriptions (
            subscription_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id                 UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            original_transaction_id TEXT NOT NULL UNIQUE,
            product_id              TEXT NOT NULL,
            tier                    TEXT NOT NULL DEFAULT 'free',
            status                  TEXT NOT NULL DEFAULT 'active'
                                        CHECK (status IN ('active', 'grace', 'expired', 'revoked')),
            expires_at              TIMESTAMPTZ,
            auto_renew              BOOLEAN NOT NULL DEFAULT true,
            environment             TEXT NOT NULL DEFAULT 'Production'
                                        CHECK (environment IN ('Sandbox', 'Production')),
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON ai.subscriptions(user_id, updated_at DESC)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.iap_events (
            event_id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            notification_uuid       TEXT NOT NULL UNIQUE,
            notification_type       TEXT,
            subtype                 TEXT,
            original_transaction_id TEXT,
            received_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            raw_payload             JSONB
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_iap_events_original ON ai.iap_events(original_transaction_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.iap_events")
    op.execute("DROP TABLE IF EXISTS ai.subscriptions")
