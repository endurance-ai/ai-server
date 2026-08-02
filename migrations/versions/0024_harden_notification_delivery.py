"""harden notification delivery — endpoint ownership, outbox, per-device attempts

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-02

0023 is already applied to the shared database.  This migration therefore
preserves the existing device rows while replacing the unsafe
``(user_id, apns_token)`` ownership rule with a globally unique push endpoint.
Notification events remain the durable domain log; messages and deliveries
form the transactional outbox used by the standalone worker.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A token identifies one app installation, not one user.  Account switching
    # can leave the same token attached to multiple users under the 0012 key.
    # Keep the most recently registered owner before installing the global key.
    op.execute(
        """
        DELETE FROM ai.devices d
        USING (
            SELECT device_id,
                   row_number() OVER (
                       PARTITION BY apns_token
                       ORDER BY registered_at DESC NULLS LAST, device_id DESC
                   ) AS owner_rank
            FROM ai.devices
        ) ranked
        WHERE d.device_id = ranked.device_id
          AND ranked.owner_rank > 1
        """
    )
    op.execute("ALTER TABLE ai.devices DROP CONSTRAINT IF EXISTS devices_user_id_apns_token_key")
    op.execute("ALTER TABLE ai.devices RENAME COLUMN apns_token TO push_token")
    op.execute(
        """
        ALTER TABLE ai.devices
            ADD COLUMN provider TEXT NOT NULL DEFAULT 'apns',
            ADD COLUMN environment TEXT NOT NULL DEFAULT 'production',
            ADD COLUMN topic TEXT NOT NULL DEFAULT 'com.kikoai.app',
            ADD COLUMN status TEXT NOT NULL DEFAULT 'active',
            ADD COLUMN last_seen_at TIMESTAMPTZ,
            ADD COLUMN disabled_at TIMESTAMPTZ,
            ADD COLUMN invalidated_at TIMESTAMPTZ,
            ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0,
            ADD COLUMN last_error TEXT
        """
    )
    op.execute("UPDATE ai.devices SET last_seen_at = COALESCE(registered_at, now())")
    op.execute("ALTER TABLE ai.devices ALTER COLUMN last_seen_at SET NOT NULL")
    op.execute(
        """
        ALTER TABLE ai.devices
            ADD CONSTRAINT devices_provider_check CHECK (provider IN ('apns', 'fcm')),
            ADD CONSTRAINT devices_environment_check CHECK (environment IN ('development', 'production')),
            ADD CONSTRAINT devices_status_check CHECK (status IN ('active', 'inactive', 'quarantined'))
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_devices_push_endpoint
        ON ai.devices (provider, environment, topic, push_token)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_devices_active_user
        ON ai.devices (user_id, provider, environment)
        WHERE status = 'active'
        """
    )

    # Events are deduplicated by their product semantics. Brand-new events are
    # lifetime-once; saved-product transitions can alert again on another day.
    op.execute("DROP INDEX IF EXISTS ai.uq_notifications_daily")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_saved_daily
        ON ai.notifications (user_id, kind, product_id, created_on)
        WHERE kind IN ('restock', 'price_drop')
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_brand_product
        ON ai.notifications (user_id, kind, product_id)
        WHERE kind = 'brand_new_product'
        """
    )
    op.execute(
        """
        ALTER TABLE ai.notifications
            ADD COLUMN processed_at TIMESTAMPTZ,
            ADD COLUMN suppressed_reason TEXT
        """
    )

    op.execute(
        """
        CREATE TABLE ai.notification_messages (
            message_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            category        TEXT NOT NULL CHECK (category IN ('saved_product_digest', 'brand_new_digest')),
            scheduled_on    DATE NOT NULL,
            scheduled_at    TIMESTAMPTZ NOT NULL,
            expires_at      TIMESTAMPTZ NOT NULL,
            title           TEXT NOT NULL,
            body            TEXT NOT NULL,
            payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'processing', 'accepted', 'partial',
                                              'failed', 'expired', 'no_recipient')),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at    TIMESTAMPTZ,
            UNIQUE (user_id, category, scheduled_on)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE ai.notification_message_events (
            message_id      UUID NOT NULL REFERENCES ai.notification_messages(message_id) ON DELETE CASCADE,
            notification_id BIGINT NOT NULL REFERENCES ai.notifications(id) ON DELETE CASCADE,
            PRIMARY KEY (message_id, notification_id),
            UNIQUE (notification_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE ai.notification_deliveries (
            delivery_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            message_id      UUID NOT NULL REFERENCES ai.notification_messages(message_id) ON DELETE CASCADE,
            device_id       UUID REFERENCES ai.devices(device_id) ON DELETE SET NULL,
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'processing', 'retry', 'accepted',
                                              'failed', 'expired')),
            attempt_count   INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            leased_until    TIMESTAMPTZ,
            apns_id         UUID NOT NULL DEFAULT gen_random_uuid(),
            last_http_status INTEGER,
            last_reason     TEXT,
            last_attempt_at TIMESTAMPTZ,
            accepted_at     TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (message_id, device_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_notification_deliveries_due
        ON ai.notification_deliveries (next_attempt_at, delivery_id)
        WHERE status IN ('pending', 'retry', 'processing')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_notification_messages_user
        ON ai.notification_messages (user_id, scheduled_at DESC)
        """
    )

    op.execute(
        """
        ALTER TABLE ai.notification_job_state
            ADD COLUMN last_started_at TIMESTAMPTZ,
            ADD COLUMN last_succeeded_at TIMESTAMPTZ,
            ADD COLUMN heartbeat_at TIMESTAMPTZ,
            ADD COLUMN last_error TEXT,
            ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )
    op.execute(
        """
        CREATE INDEX idx_user_brand_picks_brand
        ON ai.user_brand_picks (brand_id, user_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ai.idx_user_brand_picks_brand")
    op.execute(
        """
        ALTER TABLE ai.notification_job_state
            DROP COLUMN IF EXISTS metadata,
            DROP COLUMN IF EXISTS last_error,
            DROP COLUMN IF EXISTS heartbeat_at,
            DROP COLUMN IF EXISTS last_succeeded_at,
            DROP COLUMN IF EXISTS last_started_at
        """
    )
    op.execute("DROP TABLE IF EXISTS ai.notification_deliveries")
    op.execute("DROP TABLE IF EXISTS ai.notification_message_events")
    op.execute("DROP TABLE IF EXISTS ai.notification_messages")
    op.execute("ALTER TABLE ai.notifications DROP COLUMN IF EXISTS suppressed_reason")
    op.execute("ALTER TABLE ai.notifications DROP COLUMN IF EXISTS processed_at")
    op.execute("DROP INDEX IF EXISTS ai.uq_notifications_brand_product")
    op.execute("DROP INDEX IF EXISTS ai.uq_notifications_saved_daily")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_notifications_daily
        ON ai.notifications (user_id, kind, coalesce(product_id, -1), created_on)
        """
    )
    op.execute("DROP INDEX IF EXISTS ai.idx_devices_active_user")
    op.execute("DROP INDEX IF EXISTS ai.uq_devices_push_endpoint")
    op.execute("ALTER TABLE ai.devices DROP CONSTRAINT IF EXISTS devices_status_check")
    op.execute("ALTER TABLE ai.devices DROP CONSTRAINT IF EXISTS devices_environment_check")
    op.execute("ALTER TABLE ai.devices DROP CONSTRAINT IF EXISTS devices_provider_check")
    op.execute(
        """
        ALTER TABLE ai.devices
            DROP COLUMN IF EXISTS last_error,
            DROP COLUMN IF EXISTS failure_count,
            DROP COLUMN IF EXISTS invalidated_at,
            DROP COLUMN IF EXISTS disabled_at,
            DROP COLUMN IF EXISTS last_seen_at,
            DROP COLUMN IF EXISTS status,
            DROP COLUMN IF EXISTS topic,
            DROP COLUMN IF EXISTS environment,
            DROP COLUMN IF EXISTS provider
        """
    )
    op.execute("ALTER TABLE ai.devices RENAME COLUMN push_token TO apns_token")
    op.execute(
        """
        DELETE FROM ai.devices d
        USING (
            SELECT device_id,
                   row_number() OVER (
                       PARTITION BY user_id, push_token
                       ORDER BY registered_at DESC NULLS LAST, device_id DESC
                   ) AS keep_rank
            FROM ai.devices
        ) ranked
        WHERE d.device_id = ranked.device_id AND ranked.keep_rank > 1
        """
    )
    op.execute("ALTER TABLE ai.devices ADD CONSTRAINT devices_user_id_apns_token_key UNIQUE (user_id, apns_token)")
