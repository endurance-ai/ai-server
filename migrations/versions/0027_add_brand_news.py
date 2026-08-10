"""브랜드 소식 정본(canonical) 테이블 — 브랜드 홈 노출의 단일 출처

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-09

0025/0026 까지 브랜드 세일 소식은 **유저별 팬아웃 행으로만** 존재했다. 감지는
`_BRAND_SALE_SQL` 이 notify_enabled 팔로워가 있는 브랜드만 훑고, 결과를 팔로워 수만큼
복제해 ai.notifications 에 넣는 구조다. 그래서 브랜드 홈에 소식을 띄우려 하면 두 가지가
막힌다.

  ① **브랜드 단위 정본이 없다.** "이 브랜드가 세일을 시작했다"는 사실이 팔로워 N명의
     알림 행 N개로만 남아, 어느 행이 브랜드의 소식인지 지정할 수 없다.
  ② **팔로워 0명 브랜드는 소식이 생성되지 않는다.** followers CTE 가 JOIN 이라
     아무도 팔로우하지 않은 브랜드는 집계 대상에서 빠진다. 브랜드 홈은 비로그인도
     보는 페이지(get_optional_user_id)인데 노출할 컨텐츠가 팔로우 여부에 종속된다.

ai.brand_news 는 그 정본이다. 감지 순서를 "팔로워 → 감지 → 유저 알림" 에서
"감지 → 브랜드 소식 저장 → 팔로워 팬아웃" 으로 뒤집고, 브랜드 홈은 이 테이블만 읽는다.
유저별 알림 행은 그대로 남되 brand_news_id 로 정본을 가리킨다.

- ai.brand_news: 브랜드 × 소식 1행. 유저 수와 무관하게 O(브랜드 × 이벤트) 로 증가한다.
  started_at/ended_at 로 기간을 표현한다 — ai.brand_sale_state 는 "지금 세일 중인가"
  라는 현재 상태만 upsert 로 덮어써서 지난 소식을 재구성할 수 없었다. 상태 테이블은
  전환 게이트로 계속 쓰고, 이력은 여기가 갖는다.
- uq_brand_news_open: 진행 중(ended_at IS NULL)인 소식은 브랜드·종류당 최대 1건.
  open/close 를 멱등으로 만들어 같은 배치를 두 번 돌려도 소식이 늘지 않는다.
  brand_sale 의 중복 방지가 brand_sale_state 전환 게이트 하나에만 의존하던 것에
  두 번째 방어선을 준다.
- ai.notifications.brand_news_id: 인박스 행 → 정본 역참조. ON DELETE SET NULL 로
  소식을 지워도 유저의 알림 이력은 남는다.

kind CHECK 는 'brand_sale' 만 허용한다. brand_new_product 는 아직 브랜드 단위 컨텐츠가
아니다 — 상품별 × 유저 성별 매칭 결과라 정본으로 접을 수 없다. 나중에 브랜드 단위
"신상 N개 입고" 소식으로 접을 때 0025/0026 과 같은 방식으로 CHECK 를 확장한다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ai.brand_news (
            id            BIGSERIAL PRIMARY KEY,
            brand_node_id BIGINT NOT NULL,
            kind          TEXT NOT NULL CHECK (kind IN ('brand_sale')),
            payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
            started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at      TIMESTAMPTZ,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # 브랜드 홈 피드 — brand_node_id 로 좁히고 최신순 LIMIT N. 커버링 순서 그대로.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_brand_news_feed
        ON ai.brand_news (brand_node_id, started_at DESC)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_brand_news_open
        ON ai.brand_news (brand_node_id, kind)
        WHERE ended_at IS NULL
        """
    )

    op.execute(
        """
        ALTER TABLE ai.notifications
            ADD COLUMN IF NOT EXISTS brand_news_id BIGINT
            REFERENCES ai.brand_news(id) ON DELETE SET NULL
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE ai.notifications DROP COLUMN IF EXISTS brand_news_id")
    op.execute("DROP INDEX IF EXISTS ai.uq_brand_news_open")
    op.execute("DROP INDEX IF EXISTS ai.idx_brand_news_feed")
    op.execute("DROP TABLE IF EXISTS ai.brand_news")
