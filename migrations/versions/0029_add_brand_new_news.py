"""브랜드 홈 신상 요약 — ai.brand_news 에 'brand_new' 종류 추가

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-09

브랜드 홈의 "최근 소식" 이 세일 소식만 읽어서, 세일 중이 아닌 대부분의 브랜드는 이 칸이
비어 보였다. 세일은 자주 있는 일이 아니다. "이번 주 신상 N개" 요약을 같은 정본 테이블에
얹어 브랜드 홈을 채운다.

**알림(푸시·알림함)은 건드리지 않는다.** brand_new_product 는 상품별 × 유저 성별
매칭 결과라 정본으로 접을 수 없다 — 남자 유저에겐 셔츠 3개, 여자 유저에겐 원피스 10개가
가야 하는데 공유 행 하나로는 그걸 표현하지 못한다. 그래서 알림은 지금의 상품별 팬아웃을
그대로 두고, 성별 매칭이 애초에 의미 없는 화면(브랜드 홈은 비로그인도 본다)에만 브랜드
단위 요약을 노출한다.

그 결과 ai.brand_news 의 두 종류는 소비자가 다르다:
  brand_sale — 브랜드 홈 + 알림함(read fan-out)
  brand_new  — 브랜드 홈 **전용**. 알림함은 상품별 행을 따로 갖고 있어 여기 끼면 중복이다.
알림함 쿼리(app/api/notifications.py `_INBOX_NEWS_KINDS`)가 종류로 걸러낸다.

세일과 달리 신상 요약은 on/off 전환이 아니라 **롤링 윈도우 집계**다. 최근 N일 신상이
있으면 열린 소식 1건을 유지하며 개수만 갱신하고, 윈도우가 비면 닫는다. uq_brand_news_open
(0027)이 브랜드·종류당 열린 소식을 1건으로 강제하므로 열기/갱신이 upsert 한 방이 된다.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai.brand_news DROP CONSTRAINT IF EXISTS brand_news_kind_check")
    op.execute(
        """
        ALTER TABLE ai.brand_news
            ADD CONSTRAINT brand_news_kind_check
            CHECK (kind IN ('brand_sale', 'brand_new'))
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM ai.brand_news WHERE kind = 'brand_new'")
    op.execute("ALTER TABLE ai.brand_news DROP CONSTRAINT IF EXISTS brand_news_kind_check")
    op.execute(
        """
        ALTER TABLE ai.brand_news
            ADD CONSTRAINT brand_news_kind_check
            CHECK (kind IN ('brand_sale'))
        """
    )
