"""SPEC-CONVERSATION-LOG-001 / LOG-T24 — REQ-LOG-EMIT-EVERY-NODE-001.

이 테스트는 두 가지 emit-floor invariant 를 잠근다:

(a) AST: 12 graph node 파일 각각이 ≥ 1 회의 `emit(...)` 호출을 포함한다.
    SPEC §REQ-LOG-EMIT-EVERY-NODE-001 acceptance.

(b) 100-turn synthetic load: `emit()` 를 800 회 (= 100 turn × 8 row/turn 의 lower
    bound) 호출했을 때 `ai.log_conversation_event` 의 `created_at > now()-5min`
    row 수가 ≥ 800. testcontainers-postgres 가 필요; Docker 미가용 시 skip.

@MX:SPEC: SPEC-CONVERSATION-LOG-001
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
GRAPH_NODES_DIR = REPO_ROOT / "app" / "graphs" / "nodes"

# SPEC-AGENT-V2-CLEANUP-001 — the V1-only nodes (search.py, critique_apply.py,
# respond.py, taste_update.py) were deleted with the V1 topology. The emit
# floor invariant applies to the preserved deterministic-funnel + reused nodes.
# The `agent` node emits via the ReAct loop's tool_call events (react_loop.py),
# not in the node file, so it is intentionally not in this AST file list.
NODE_FILES = [
    "ingest.py",
    "resolve_image.py",
    "vision.py",
    "pick_item.py",
    "ask_clarify.py",
    "apply_clarify.py",
    "evaluator.py",
    "send_results.py",
    "intro.py",
    # SPEC-ONBOARD-LITE-001 — pinterest_ingest.py / onboard_*.py deleted with
    # the onboarding card subgraph; removed from the emit-floor invariant.
]


def _count_emit_calls(file_path: Path) -> int:
    """Return number of `emit(...)` (or `*emit(...)`) calls in the file."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id.endswith("emit"):
            count += 1
        elif isinstance(func, ast.Attribute) and func.attr.endswith("emit"):
            count += 1
    return count


# Module-level autouse override — AST tests do NOT need Docker. The PG
# synthetic-load test below explicitly depends on `conv_log_backend_postgres`
# which transitively brings in `pg_container` (skipping cleanly when Docker
# is absent).
@pytest.fixture(autouse=True)
def _truncate_log_table():
    yield


# ─────────────────── (a) AST: each node has ≥ 1 emit ────────────────────


@pytest.mark.parametrize("node_filename", NODE_FILES)
def test_each_node_contains_at_least_one_emit_call(node_filename: str):
    """REQ-LOG-EMIT-EVERY-NODE-001 — every node emits at least one event."""
    path = GRAPH_NODES_DIR / node_filename
    assert path.exists(), f"Expected node file at {path}"
    n = _count_emit_calls(path)
    assert n >= 1, f"{node_filename} contains 0 emit() calls; SPEC requires ≥ 1"


def test_total_emit_calls_across_nodes_at_least_floor():
    """Aggregate sanity check — total emit sites ≥ one floor per node."""
    total = sum(_count_emit_calls(GRAPH_NODES_DIR / f) for f in NODE_FILES)
    assert total >= len(NODE_FILES), f"Total emit() sites = {total}, expected ≥ {len(NODE_FILES)}"


# ───────────────── (b) 100-turn synthetic load smoke (PG) ───────────────


@pytest.mark.asyncio
async def test_synthetic_800_emits_lands_in_log_table(
    conv_log_backend_postgres,
    pg_dsn: str,
):
    """REQ-LOG-EMIT-EVERY-NODE-001 — 100 turns × 8 events/turn synthetic load
    produces ≥ 800 rows in `ai.log_conversation_event` within 5 min.

    We bypass the graph and call `emit()` directly 800 times so the test is
    not coupled to node implementations. Awaiting in-flight tasks ensures
    the INSERTs complete before the COUNT query.
    """
    from app.observability.conversation_log import _IN_FLIGHT, emit

    base_thread = uuid4()
    payloads_per_turn = [
        ("user_text", {"text": "hi", "lang_detected": "en"}),
        ("intent_routed", {"intent": "new_search_request"}),
        ("vision_done", {"schema_v2_used": True}),
        ("pick_item_done", {"candidate_items": [], "picked_index": 0, "auto_picked": True}),
        ("search_done", {"top_k_product_ids": ["p1"], "rrf_scores": [0.9]}),
        ("diversify_done", {"input_count": 1, "output_count": 1, "brand_cap": 1, "platform_cap": 1}),
        (
            "card_sent",
            {
                "product_id": "p1",
                "position": 0,
                "send_ok": True,
                "send_elapsed_ms": 10,
                "source_message_id": 1,
            },
        ),
        ("bot_text", {"chunk_text": "ok", "chunk_index": 0, "total_chunks": 1, "flow": "RESULTS"}),
    ]

    for turn in range(100):
        for et, payload in payloads_per_turn:
            emit(
                event_type=et,
                user_key=f"u:smoke:{turn}",
                chat_id=99000 + turn,
                thread_id=base_thread,
                turn_no=turn,
                payload=payload,
            )

    # Drain in-flight items. emit() may store either asyncio.Task (fallback path)
    # or concurrent.futures.Future (pool_loop path). asyncio.gather() only
    # accepts awaitables, so wrap concurrent.futures.Future with asyncio.wrap_future.
    for _ in range(50):
        pending = [t for t in list(_IN_FLIGHT) if not t.done()]
        if not pending:
            break
        awaitables = [t if isinstance(t, (asyncio.Task, asyncio.Future)) else asyncio.wrap_future(t) for t in pending]
        await asyncio.gather(*awaitables, return_exceptions=True)
        await asyncio.sleep(0.05)

    # Final assertion via direct COUNT.
    from app.providers.db_pool import get_pool

    pool = get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM ai.log_conversation_event WHERE created_at > now() - interval '5 minutes'"
        )
        row = await cur.fetchone()
        count = row[0]

    assert count >= 800, f"100-turn synthetic load produced {count} rows, expected ≥ 800"
