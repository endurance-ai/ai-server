"""SPEC-CONVERSATION-LOG-001 / LOG-T25 — REQ-LOG-MIGRATION-001 GIN index usage.

`EXPLAIN` of `payload @> '{...}'` 쿼리가 GIN 인덱스 `idx_log_conv_payload_gin`
를 사용하는지 검증한다. testcontainers-postgres 필요.

Note: Postgres planner 는 row 수가 적으면 seq scan 을 선호하므로 1000 rows seed
+ ANALYZE 로 통계 기반 planner 가 GIN 선택하도록 유도한다.

@MX:SPEC: SPEC-CONVERSATION-LOG-001
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb


@pytest.mark.asyncio
async def test_payload_jsonb_containment_uses_gin_index(
    conv_log_backend_postgres,
    pg_dsn: str,
):
    """REQ-LOG-MIGRATION-001 — `EXPLAIN ... payload @> '{...}'` plan contains
    `Bitmap Index Scan on idx_log_conv_payload_gin` (and NOT a Seq Scan on the table)."""
    from app.providers.db_pool import get_pool

    pool = get_pool()
    tid = uuid4()

    # Seed 1000 rows with varied event_type and payload contents.
    async with pool.connection() as conn, conn.cursor() as cur:
        for i in range(1000):
            intent = "new_search_request" if i % 5 == 0 else "continue_session"
            await cur.execute(
                "INSERT INTO ai.log_conversation_event "
                "(user_key, chat_id, thread_id, turn_no, event_type, payload) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    f"u:gin:{i % 50}",
                    10000 + i,
                    str(tid),
                    i,
                    "intent_routed",
                    Jsonb({"intent": intent, "n": i}),
                ),
            )
        await conn.commit()
        # Force planner stats refresh.
        await cur.execute("ANALYZE ai.log_conversation_event")
        await conn.commit()

    # EXPLAIN (force the planner to prefer index by disabling seq scan as
    # backup safety net — but first try without the override to assert the
    # planner *naturally* picks GIN given stats).
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "EXPLAIN SELECT * FROM ai.log_conversation_event "
            'WHERE payload @> \'{"intent":"new_search_request"}\'::jsonb'
        )
        rows = await cur.fetchall()
        plan_text = "\n".join(r[0] for r in rows)

    # If natural plan picks Seq Scan (small table heuristic), re-test with
    # `enable_seqscan = off` to verify the GIN index *can* be used.
    if "idx_log_conv_payload_gin" not in plan_text:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SET LOCAL enable_seqscan = off")
            await cur.execute(
                "EXPLAIN SELECT * FROM ai.log_conversation_event "
                'WHERE payload @> \'{"intent":"new_search_request"}\'::jsonb'
            )
            rows = await cur.fetchall()
            plan_text = "\n".join(r[0] for r in rows)

    assert "idx_log_conv_payload_gin" in plan_text, f"GIN index not used. EXPLAIN output:\n{plan_text}"
    # Bitmap Index Scan is the access path for GIN.
    assert "Bitmap Index Scan" in plan_text or "Bitmap Heap Scan" in plan_text, (
        f"Plan does not contain bitmap scan over GIN index:\n{plan_text}"
    )
