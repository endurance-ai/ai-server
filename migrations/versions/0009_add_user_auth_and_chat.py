"""add user_profiles, refresh_tokens, chat_sessions, chat_messages

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-21

소셜 로그인(Google/Apple) 전환에 필요한 소비자 유저 정체성 레이어와 채팅 영속성 테이블.

Tables added (all in ai schema):
- ai.user_profiles   — OAuth identity + tier/gender
- ai.refresh_tokens  — refresh token revocation store
- ai.chat_sessions   — chat session per user
- ai.chat_messages   — individual messages per session

Columns added to existing tables:
- ai.user_session.user_id      — nullable FK to user_profiles (gradual migration)
- ai.user_taste_profile.user_id — same
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.user_profiles (
            user_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider        TEXT NOT NULL,
            provider_id     TEXT NOT NULL,
            email           TEXT,
            display_name    TEXT,
            avatar_url      TEXT,
            gender          TEXT,
            tier            TEXT NOT NULL DEFAULT 'free',
            tier_expires_at TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (provider, provider_id),
            CHECK (tier IN ('free', 'basic', 'pro', 'premium')),
            CHECK (gender IS NULL OR gender IN ('male', 'female', 'other'))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.refresh_tokens (
            token_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id     UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            token_hash  TEXT NOT NULL UNIQUE,
            expires_at  TIMESTAMPTZ NOT NULL,
            revoked_at  TIMESTAMPTZ,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id ON ai.refresh_tokens(user_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.chat_sessions (
            session_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            title           TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_message_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_id ON ai.chat_sessions(user_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.chat_messages (
            message_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id   UUID NOT NULL REFERENCES ai.chat_sessions(session_id) ON DELETE CASCADE,
            role         TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content      TEXT NOT NULL,
            product_refs JSONB,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON ai.chat_messages(session_id)")

    # Gradual migration: link existing session/profile rows to new user identity
    op.execute("ALTER TABLE ai.user_session ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES ai.user_profiles(user_id)")
    op.execute(
        "ALTER TABLE ai.user_taste_profile ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES ai.user_profiles(user_id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ai.user_taste_profile DROP COLUMN IF EXISTS user_id")
    op.execute("ALTER TABLE ai.user_session DROP COLUMN IF EXISTS user_id")
    op.execute("DROP TABLE IF EXISTS ai.chat_messages")
    op.execute("DROP TABLE IF EXISTS ai.chat_sessions")
    op.execute("DROP TABLE IF EXISTS ai.refresh_tokens")
    op.execute("DROP TABLE IF EXISTS ai.user_profiles")
