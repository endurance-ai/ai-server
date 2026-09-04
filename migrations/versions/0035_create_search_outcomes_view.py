"""create ai.search_outcomes view — bind search outcomes to the recommendation trace

The eval loop's foundation: make "did this search change help?" measurable.

Outcome signals (view / outbound-click / save) already exist in
`ai.taste_feature_events`, and `ai.card_impression` already records which
products were shown in which `langfuse_trace` (the per-turn search identity)
with an attribution window. They were never joined, so outcomes could not be
attributed to the search that produced them.

This view does the join at query time — NO new capture, NO schema change to the
capture tables — so it works on existing history:

  taste_feature_events (user+product+time)  ──┐
                                              ├─ attributed within [shown_at, +window]
  card_impression (from_user_id, product_id, ─┘   → langfuse_trace
                   shown_at, attribution_window_s, langfuse_trace)
                                              │
  log_conversation_event (search tool_call args_summary) ── same langfuse_trace

Result grain: one row per search trace, with the search params (text_query /
brand / category / color) + impressions and attributed view/click/save counts
and rates. Group by any search dimension to compare configs.

Identity bridge: card_impression.from_user_id is the app user UUID mapped by
`app.core.identity.uuid_to_session_key` = unsigned(first 8 bytes, big-endian)
% 2**62 = the low 62 bits of the first 16 hex chars. Reproduced in SQL below.

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# NOTE: the referenced tables (card_impression, taste_feature_events,
# log_conversation_event) all live in the `ai` schema, which the migration role
# owns. No `public` object is referenced, so this succeeds in a pure-migration
# environment.
_CREATE_VIEW = """
CREATE OR REPLACE VIEW ai.search_outcomes AS
WITH tfe AS (
    -- app user UUID -> the bigint session key stored in card_impression.from_user_id
    -- (uuid_to_session_key: unsigned first-8-bytes big-endian % 2^62 = low 62 bits).
    SELECT
        ( ('x' || substr(replace(user_id::text, '-', ''), 1, 16))::bit(64)
          & b'0011111111111111111111111111111111111111111111111111111111111111' )::bigint AS sess_key,
        product_id::text AS product_id,
        signal_type,
        occurred_at
    FROM ai.taste_feature_events
    WHERE signal_type IN ('view', 'outbound', 'save', 'unsave')
),
imp_out AS (
    -- one row per impression, with the outcomes attributed inside its window
    SELECT
        i.langfuse_trace,
        i.id                                    AS impression_id,
        max((t.signal_type = 'view')::int)      AS viewed,
        max((t.signal_type = 'outbound')::int)  AS clicked,
        max((t.signal_type = 'save')::int)      AS saved,
        max((t.signal_type = 'unsave')::int)    AS unsaved
    FROM ai.card_impression i
    LEFT JOIN tfe t
           ON t.sess_key   = i.from_user_id
          AND t.product_id = i.product_id
          AND t.occurred_at BETWEEN i.shown_at
                                AND i.shown_at + make_interval(secs => COALESCE(i.attribution_window_s, 3600))
    WHERE i.langfuse_trace IS NOT NULL
    GROUP BY i.langfuse_trace, i.id
),
search_args AS (
    -- the FIRST search tool call of the trace supplies the search dimensions
    SELECT DISTINCT ON (langfuse_trace)
        langfuse_trace,
        payload->>'tool_name'                     AS tool_name,
        payload->'args_summary'->>'text_query'    AS text_query,
        payload->'args_summary'->>'brand'         AS brand,
        payload->'args_summary'->>'category'      AS category,
        payload->'args_summary'->>'color_family'  AS color_family,
        created_at                                AS searched_at
    FROM ai.log_conversation_event
    WHERE event_type = 'tool_call'
      AND payload->>'tool_name' IN ('search_products', 'refine_search')
    ORDER BY langfuse_trace, created_at ASC
)
SELECT
    o.langfuse_trace,
    sa.searched_at,
    sa.tool_name,
    sa.text_query,
    sa.brand,
    sa.category,
    sa.color_family,
    count(*)                                                     AS impressions,
    COALESCE(sum(o.viewed),  0)                                  AS views,
    COALESCE(sum(o.clicked), 0)                                  AS clicks,
    COALESCE(sum(o.saved),   0)                                  AS saves,
    COALESCE(sum(o.unsaved), 0)                                  AS unsaves,
    round(sum(o.viewed)::numeric  / nullif(count(*), 0), 4)      AS view_rate,
    round(sum(o.clicked)::numeric / nullif(count(*), 0), 4)      AS click_rate,
    round(sum(o.saved)::numeric   / nullif(count(*), 0), 4)      AS save_rate
FROM imp_out o
LEFT JOIN search_args sa ON sa.langfuse_trace = o.langfuse_trace
GROUP BY o.langfuse_trace, sa.searched_at, sa.tool_name,
         sa.text_query, sa.brand, sa.category, sa.color_family;
"""


def upgrade() -> None:
    op.execute(_CREATE_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS ai.search_outcomes;")
