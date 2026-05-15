"""SPEC-CONVERSATION-LOG-001 / LOG-T25 — REQ-LOG-PRIVACY-001 user_key isolation.

GDPR ad-hoc delete path: `DELETE FROM ai.log_conversation_event WHERE user_key=$1`
삭제 시 다른 사용자의 row 가 영향받지 않는다. testcontainers-postgres 필요.

@MX:SPEC: SPEC-CONVERSATION-LOG-001
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb


@pytest.mark.asyncio
async def test_delete_by_user_key_isolates_target_user(
    conv_log_backend_postgres,
    pg_dsn: str,
):
    """REQ-LOG-PRIVACY-001 — `DELETE WHERE user_key=$1` removes only the
    target user's rows; other users' rows remain intact."""
    from app.providers.db_pool import get_pool

    pool = get_pool()
    tid = uuid4()

    # Seed 5 rows for u:42 and 5 rows for u:99.
    async with pool.connection() as conn, conn.cursor() as cur:
        for user_key, chat_id in [("u:42", 4200), ("u:99", 9900)]:
            for i in range(5):
                await cur.execute(
                    "INSERT INTO ai.log_conversation_event "
                    "(user_key, chat_id, thread_id, turn_no, event_type, payload) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_key, chat_id, str(tid), i, "user_text", Jsonb({"text": f"row {i}"})),
                )
        await conn.commit()

    # Pre-condition: 10 rows total.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT user_key, count(*) FROM ai.log_conversation_event GROUP BY user_key ORDER BY user_key"
        )
        rows = await cur.fetchall()
    assert {r[0]: r[1] for r in rows} == {"u:42": 5, "u:99": 5}

    # GDPR delete for u:42.
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM ai.log_conversation_event WHERE user_key = %s", ("u:42",))
        await conn.commit()

    # Post-condition: u:42 → 0, u:99 → 5 (unchanged).
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM ai.log_conversation_event WHERE user_key = 'u:42'")
        u42 = (await cur.fetchone())[0]
        await cur.execute("SELECT count(*) FROM ai.log_conversation_event WHERE user_key = 'u:99'")
        u99 = (await cur.fetchone())[0]

    assert u42 == 0, f"u:42 still has {u42} rows after DELETE"
    assert u99 == 5, f"u:99 row count {u99} != 5 (cross-user leakage!)"
