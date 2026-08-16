"""index the 30-second expiry sweep on ai.notification_messages

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-16

`claim_due_deliveries` 는 워커 폴 간격(기본 30초)마다 만료 스윕 두 개를 돌린다. 둘 다
`ai.notification_messages.expires_at` 으로 거르는데 그 컬럼을 받는 인덱스가 없어서
**매번 테이블 전체를 seq scan** 했다 (하루 2,880회).

    Seq Scan on notification_messages m
      Filter: ((expires_at <= $1) AND (status = ANY ('{pending,processing}')))

메시지 행은 유저·카테고리·날짜당 1건씩 영구히 쌓인다 — 유저 1만이면 연 100만 행 규모라
스윕 비용이 누적 행 수에 정비례한다. 딜리버리 쪽은 이미
`idx_notification_deliveries_due ... WHERE status IN ('pending','retry','processing')` 부분
인덱스가 살아있는 행만 보게 해 두었고, 이 마이그레이션은 메시지 쪽에 같은 처방을 한다.

부분 인덱스인 이유: 스윕이 찾는 건 아직 마감되지 않은 메시지뿐이고, 행은 마감되면
(accepted/partial/failed/expired/no_recipient) 술어에서 빠져 인덱스에서 사라진다. 즉
인덱스 크기가 누적 행 수가 아니라 **진행 중인 메시지 수**에 묶인다 — 테이블이 아무리
커져도 인덱스는 하루치 규모를 넘지 않는다.

CONCURRENTLY 를 쓰지 않는 이유: 이 스키마의 다른 마이그레이션과 같은 트랜잭션 정책을
따른다. 대상 테이블이 현재 71행이라 잠금 시간이 무시할 수준이다. 테이블이 커진 뒤에
적용한다면 CONCURRENTLY 로 바꿔야 한다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_notification_messages_expiry
        ON ai.notification_messages (expires_at)
        WHERE status IN ('pending', 'processing')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ai.idx_notification_messages_expiry")
