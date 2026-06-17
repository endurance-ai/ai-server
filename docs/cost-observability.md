# Cost Observability Guide

This document defines the cost source of truth and the queries used to validate
LLM spend for pricing-plan decisions.

## Source of Truth

Use `ai.log_conversation_event` rows with `event_type = 'llm_call'` as the cost
ledger.

- `llm_call` is emitted once per observed LLM call.
- `payload.turn_id` groups all LLM calls that belong to one user turn.
- `payload.cost_usd` uses LiteLLM `response_cost` when available.
- `payload.cost_source = 'fallback_rates'` means the app estimated cost from
  local model rates because the proxy response did not include cost.
- `turn_summary.cost_usd` is a per-turn rollup and should match the sum of
  `llm_call.cost_usd` for the same `turn_id`.

Langfuse is the observability and reconciliation layer, not the billing source
of truth. Global Langfuse `Total cost` can include standalone LiteLLM traces,
unknown traces, debug calls, and other traces outside the app's turn boundary.
For validation, filter Langfuse by `metadata.turn_id`.

## Langfuse Fields to Check

Open a `webhook.telegram` trace and inspect metadata:

- `turn_id`: join key for DB cost ledger and Langfuse trace.
- `cost_usd`: per-turn rollup from the app accumulator.
- `llm_call_count`: number of LLM calls observed in the turn.
- `conversation_flow`: compact timeline of the turn.
- `conversation_last_event`: last recorded event in the turn.
- `conversation_event_count`: breadcrumb count for quick completeness checks.

Expected relationship:

```text
1 user turn = 1 turn_id = 1 webhook.telegram root trace = N llm_call rows
```

## Daily Cost

```sql
SELECT
    date_trunc('day', created_at) AS day,
    COUNT(*) AS llm_calls,
    COUNT(DISTINCT payload->>'turn_id') AS turns,
    ROUND(SUM((payload->>'cost_usd')::float)::numeric, 6) AS cost_usd,
    SUM((payload->>'total_tokens')::int) AS total_tokens
FROM ai.log_conversation_event
WHERE event_type = 'llm_call'
  AND created_at >= NOW() - INTERVAL '14 days'
GROUP BY 1
ORDER BY 1 DESC;
```

## Turn-Level Cost Distribution

```sql
WITH turn_costs AS (
    SELECT
        payload->>'turn_id' AS turn_id,
        COUNT(*) AS llm_calls,
        SUM((payload->>'total_tokens')::int) AS total_tokens,
        SUM((payload->>'cost_usd')::float) AS cost_usd
    FROM ai.log_conversation_event
    WHERE event_type = 'llm_call'
      AND created_at >= NOW() - INTERVAL '7 days'
      AND payload->>'turn_id' IS NOT NULL
    GROUP BY 1
)
SELECT
    COUNT(*) AS turns,
    ROUND(AVG(total_tokens)) AS avg_tokens,
    ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY total_tokens)) AS p50_tokens,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY total_tokens)) AS p95_tokens,
    ROUND(AVG(cost_usd)::numeric, 6) AS avg_cost_usd,
    ROUND(SUM(cost_usd)::numeric, 6) AS total_cost_usd,
    ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY cost_usd)::numeric, 6) AS p95_cost_usd,
    ROUND(AVG(llm_calls)::numeric, 2) AS avg_llm_calls
FROM turn_costs;
```

## Model Breakdown

```sql
SELECT
    payload->>'model' AS model,
    payload->>'cost_source' AS cost_source,
    COUNT(*) AS llm_calls,
    COUNT(DISTINCT payload->>'turn_id') AS turns,
    SUM((payload->>'total_tokens')::int) AS total_tokens,
    SUM((payload->>'cache_read_tokens')::int) AS cache_read_tokens,
    ROUND(SUM((payload->>'cost_usd')::float)::numeric, 6) AS cost_usd
FROM ai.log_conversation_event
WHERE event_type = 'llm_call'
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY 1, 2
ORDER BY cost_usd DESC;
```

## Reconcile `llm_call` and `turn_summary`

This query finds turns where the call-level ledger and turn rollup diverge.

```sql
WITH calls AS (
    SELECT
        payload->>'turn_id' AS turn_id,
        COUNT(*) AS llm_call_count,
        SUM((payload->>'total_tokens')::int) AS call_tokens,
        SUM((payload->>'cost_usd')::float) AS call_cost
    FROM ai.log_conversation_event
    WHERE event_type = 'llm_call'
      AND created_at >= NOW() - INTERVAL '7 days'
      AND payload->>'turn_id' IS NOT NULL
    GROUP BY 1
),
summaries AS (
    SELECT
        payload->>'turn_id' AS turn_id,
        (payload->>'llm_call_count')::int AS summary_llm_calls,
        (payload->>'total_tokens')::int AS summary_tokens,
        (payload->>'cost_usd')::float AS summary_cost
    FROM ai.log_conversation_event
    WHERE event_type = 'turn_summary'
      AND created_at >= NOW() - INTERVAL '7 days'
      AND payload->>'turn_id' IS NOT NULL
)
SELECT
    COALESCE(c.turn_id, s.turn_id) AS turn_id,
    c.llm_call_count,
    s.summary_llm_calls,
    c.call_tokens,
    s.summary_tokens,
    ROUND(c.call_cost::numeric, 8) AS call_cost,
    ROUND(s.summary_cost::numeric, 8) AS summary_cost,
    ROUND((COALESCE(c.call_cost, 0) - COALESCE(s.summary_cost, 0))::numeric, 8) AS cost_delta
FROM calls c
FULL OUTER JOIN summaries s ON s.turn_id = c.turn_id
WHERE ABS(COALESCE(c.call_cost, 0) - COALESCE(s.summary_cost, 0)) > 0.000001
   OR COALESCE(c.llm_call_count, -1) <> COALESCE(s.summary_llm_calls, -1)
ORDER BY ABS(COALESCE(c.call_cost, 0) - COALESCE(s.summary_cost, 0)) DESC
LIMIT 100;
```

## Missing Cost Coverage

Use this to identify calls whose model was unknown to the fallback price table
or whose proxy response did not include LiteLLM cost.

```sql
SELECT
    payload->>'model' AS model,
    payload->>'cost_source' AS cost_source,
    COUNT(*) AS llm_calls,
    SUM((payload->>'total_tokens')::int) AS total_tokens,
    ROUND(SUM((payload->>'cost_usd')::float)::numeric, 6) AS cost_usd
FROM ai.log_conversation_event
WHERE event_type = 'llm_call'
  AND created_at >= NOW() - INTERVAL '7 days'
  AND payload->>'cost_source' <> 'litellm'
GROUP BY 1, 2
ORDER BY llm_calls DESC;
```

## Cache Efficiency

```sql
SELECT
    payload->>'model' AS model,
    SUM((payload->>'total_tokens')::int) AS total_tokens,
    SUM((payload->>'cache_read_tokens')::int) AS cache_read_tokens,
    ROUND(
        100.0 * SUM((payload->>'cache_read_tokens')::float)
        / NULLIF(SUM((payload->>'total_tokens')::float), 0),
        2
    ) AS cache_hit_rate_pct,
    ROUND(SUM((payload->>'cost_usd')::float)::numeric, 6) AS cost_usd
FROM ai.log_conversation_event
WHERE event_type = 'llm_call'
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY cost_usd DESC;
```

## Langfuse Reconciliation Procedure

1. Pick a time window and run the daily or turn-level DB query.
2. In Langfuse, filter traces by `metadata.turn_id exists`.
3. For a spot check, pick one `turn_id`.
4. Compare:
   - DB `llm_call` sum for `turn_id`
   - DB `turn_summary.cost_usd` for `turn_id`
   - Langfuse model cost after filtering by the same `turn_id`
5. If Langfuse global total is higher, inspect trace names such as
   `litellm-acomp...` or `Unknown`; those are outside the app turn boundary
   unless they also carry the same `turn_id`.

## Pricing Plan Inputs

Use these metrics for plan design:

- `avg_cost_usd` and `p95_cost_usd` per turn.
- turns per active user per day.
- model-level cost mix.
- percentage of `cost_source <> 'litellm'`.
- cache hit rate by model.
- exhausted/stuck turn rate from `turn_summary.status`.
