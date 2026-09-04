"""create ai.search_outcome_buckets — 윤영 검색 쿼리 지형도의 상시 대시보드화

윤영님이 439건을 LLM 으로 수동 분류해 만든 "표현 방식 버킷 × 전환율" 우선순위
표를, `ai.search_outcomes`(0035, 아웃컴↔trace 귀속) 위에 올려 상시 SQL 뷰로
만든다. 버킷을 `GROUP BY` 하면 우선순위표(볼륨 × 전환율)가 그대로 나온다.

grain: 검색 trace 1행. 컬럼 = 버킷 + (있으면) raw user_text + 전환 카운트.

버킷 파생: 에이전트가 각 쿼리를 구조화한 `args_summary`(brand/mood/fit/color/…)
에서 우선순위로 결정. 윤영 택소노미 매핑:
    brand present            → 'brand'      (브랜드직접명시/레퍼런스)
    mood present             → 'mood'       (무드레퍼런스)
    any attribute present    → 'attribute'  (상품속성: fit/color/material/…)
    text_query only          → 'bare_item'  (맨몸 품목)
    else                     → 'other'

⚠️ 한계(정직): args 는 "에이전트가 구조화한 것"이라 실측상 과소기입된다 — 특히
`mood` arg 는 30일 2건뿐(에이전트가 무드를 text_query 에 묻음). 따라서 이 뷰의
'mood' 버킷은 실제 무드 쿼리(윤영 분류 65건)보다 적게 잡힌다. 그래서 raw
`user_text`(catalog #1, 2026-09-04 로깅 시작)를 함께 노출해 사후 재분류/스팟체크가
가능하게 한다. 충실한 버킷팅은 user_text 누적 후(또는 LLM 배치 분류)로 승격.
(mood 텍스트추출 rerank[A]는 런타임 target_attrs 만 바꾸고 이 로그엔 안 남는다.)

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ATTR_KEYS = (
    "fit",
    "color_family",
    "color",
    "material",
    "pattern",
    "neckline",
    "length",
    "sleeve_length",
    "leg_shape",
    "surface",
    "texture",
    "design_details",
)


def _nonempty(expr: str) -> str:
    return f"({expr} IS NOT NULL AND {expr} <> '')"


_CREATE_VIEW = f"""
CREATE OR REPLACE VIEW ai.search_outcome_buckets AS
WITH args AS (
    SELECT DISTINCT ON (langfuse_trace)
        langfuse_trace,
        payload->'args_summary' AS a,
        created_at             AS searched_at
    FROM ai.log_conversation_event
    WHERE event_type = 'tool_call'
      AND payload->>'tool_name' IN ('search_products', 'refine_search')
    ORDER BY langfuse_trace, created_at ASC
),
utext AS (
    -- raw 유저 입력(catalog #1, 2026-09-04~). trace 당 최초 user_text.
    SELECT DISTINCT ON (langfuse_trace)
        langfuse_trace,
        payload->>'text' AS user_text
    FROM ai.log_conversation_event
    WHERE event_type = 'user_text'
    ORDER BY langfuse_trace, created_at ASC
),
bucketed AS (
    SELECT
        langfuse_trace,
        searched_at,
        a->>'text_query'  AS text_query,
        a->>'brand'       AS brand,
        a->>'mood'        AS mood,
        a->>'category'    AS category,
        CASE
            WHEN {_nonempty("a->>'brand'")} THEN 'brand'
            WHEN {_nonempty("a->>'mood'")} THEN 'mood'
            WHEN {" OR ".join(_nonempty(f"a->>'{k}'") for k in _ATTR_KEYS)} THEN 'attribute'
            WHEN {_nonempty("a->>'text_query'")} THEN 'bare_item'
            ELSE 'other'
        END AS bucket
    FROM args
)
SELECT
    b.langfuse_trace,
    b.searched_at,
    b.bucket,
    u.user_text,
    b.text_query,
    b.brand,
    b.mood,
    b.category,
    o.impressions,
    o.views,
    o.clicks,
    o.saves,
    o.view_rate,
    o.click_rate,
    o.save_rate
FROM bucketed b
LEFT JOIN ai.search_outcomes o USING (langfuse_trace)
LEFT JOIN utext u USING (langfuse_trace);
"""


def upgrade() -> None:
    op.execute(_CREATE_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS ai.search_outcome_buckets;")
