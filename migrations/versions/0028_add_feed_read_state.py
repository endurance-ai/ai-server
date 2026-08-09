"""인박스 read fan-out 을 위한 읽음 상태 — 워터마크 + 예외 테이블

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-09

0027 이 브랜드 소식을 ai.brand_news 정본으로 만들었다. 이제 인박스가 그 정본을
**조회**하도록 바꾼다(read fan-out) — 유저별로 복제된 ai.notifications 행을 읽는 대신.

왜 바꾸나. 지금 인박스는 "푸시 나간 것의 로그" 다. _persist_outbox 가 select_for_delivery
를 통과한 이벤트만 ai.notifications 에 적재하므로, 주간 캡에 걸린 날이나 동의를 끈
유저는 **알림함에서도** 소식이 사라진다. 유저는 푸시만 끄고 싶었는데 컨텐츠까지 사라진다.
정본을 조회하는 뷰가 되면 푸시 정책이 컨텐츠 가시성에 영향을 줄 수 없다.

저장량 축도 바뀐다: 브랜드 × 소식 × 팔로워(유저 수에 비례) → 브랜드 × 소식(유저 수와 무관).
읽기 비용은 유저 수가 아니라 한 유저의 팔로우 수에만 비례하는데, 브랜드 팔로우는
팬인 차수 상한이 낮아(수십 개) 인덱스 스캔 + LIMIT 조기 종료로 끝난다.

read fan-out 의 유일한 실비용이 읽음 상태다. 공유 행에는 유저별 read_at 을 달 수 없다.

- ai.user_feed_state: 유저별 "여기까지 읽었다" 워터마크. '전체 읽음' 이 미읽음 전량
  UPDATE 에서 1행 UPDATE 로 바뀐다 — 지금보다 오히려 싸다.
- ai.feed_reads: 워터마크로 표현 못 하는 **개별 읽음** 예외. 워터마크가 전진하면
  그 이전 행은 중복이므로 정리한다(mark_read all 경로에서 삭제).

ai.notifications.read_at 은 그대로 둔다. 개인 이벤트(restock/price_drop/brand_new)는
여전히 유저별 행이라 행마다 읽음 표시를 다는 게 자연스럽다. 자료 모양이 다르면
읽음 메커니즘도 다른 게 맞다 — 억지로 하나로 합치면 기존 read_at 백필이 필요해진다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # '-infinity' 기본값 — 행이 없는 유저와 같은 의미(아무것도 안 읽음)라 조회 쪽에서
    # coalesce 한 번으로 통일된다.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.user_feed_state (
            user_id      UUID PRIMARY KEY REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            last_read_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.feed_reads (
            user_id  UUID NOT NULL REFERENCES ai.user_profiles(user_id) ON DELETE CASCADE,
            source   TEXT NOT NULL CHECK (source IN ('brand_news')),
            ref_id   BIGINT NOT NULL,
            read_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, source, ref_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai.feed_reads")
    op.execute("DROP TABLE IF EXISTS ai.user_feed_state")
