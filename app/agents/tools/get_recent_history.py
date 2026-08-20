"""SPEC-AGENT-V2-REACT / T-003f — `get_recent_history` tool wrapper.

Read-only SELECT on `ai.log_conversation_event` with per-event-type whitelist
(OQ-8). In-memory backend → empty list fail-soft.

@MX:NOTE: [AUTO] Side effect: read-only DB SELECT.
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.agents.tool_registry import GetRecentHistoryResult

logger = logging.getLogger(__name__)


# Per-event-type whitelist — OQ-8 §8.
def _summarize_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    p = payload or {}
    # Text caps widened 200→500 (2026-05-20) so the agent has enough recent
    # phrasing to reason about user corrections like "girl 말고 남자" without
    # losing nuance to truncation. Still bounded by AGENT_V3_MEMORY_MAX_TOKENS
    # at the system-prompt assembly site.
    if event_type == "user_text":
        return {"text": (p.get("text") or "")[:500]}
    if event_type == "user_photo":
        url = p.get("image_url") or ""
        hashed = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:16] if url else None
        return {"image_url_hash": hashed}
    if event_type == "user_callback":
        return {"callback_data": p.get("callback_data")}
    if event_type == "intent_routed":
        return {"intent": p.get("intent"), "critique_delta_summary": p.get("critique_delta_summary")}
    if event_type == "vision_done":
        return {
            "style_node_primary": p.get("style_node_primary"),
            "mood": list(p.get("mood") or [])[:3],
            "items_count": len(p.get("items") or []),
        }
    if event_type == "search_done":
        # 2026-05-20: expose filters (category/subcategory/brand_names) and the
        # refine signal so the agent can detect "what did we already try" and
        # avoid repeating identical constraints across turns.
        q = p.get("query") or {}
        filters = p.get("filters") or {}
        if not isinstance(filters, dict):
            filters = {}
        return {
            "text_query": (q.get("text_query") or "")[:200] if isinstance(q, dict) else "",
            "category": filters.get("category") or filters.get("p_category"),
            "subcategory": filters.get("subcategory") or filters.get("p_subcategory"),
            "brand_names": list(filters.get("brand_names") or filters.get("p_brand_names") or [])[:5],
            "is_refine": bool(p.get("is_refine")),
            "top_k_product_ids": list(p.get("top_k_product_ids") or [])[:5],
            "raw_count": p.get("dense_count"),
        }
    if event_type == "diversify_done":
        return {"final_count": p.get("output_count")}
    if event_type == "card_sent":
        return {"product_id": p.get("product_id"), "position": p.get("position")}
    if event_type == "card_clicked":
        return {"product_id": p.get("product_id"), "position": p.get("position")}
    if event_type == "bot_text":
        return {"text": (p.get("chunk_text") or "")[:500]}
    if event_type == "taste_update":
        return {"applied": True}
    if event_type == "tool_call":
        # 2026-05-20: surface args_summary so the agent can see what filters
        # were applied in prior tool calls (e.g., search_products text_query +
        # category), enabling smarter refine decisions across turns.
        return {
            "tool_name": p.get("tool_name"),
            "iteration_no": p.get("iteration_no"),
            "latency_ms": p.get("latency_ms"),
            "error": p.get("error"),
            "args_summary": p.get("args_summary"),
        }
    if event_type == "ask_clarify_sent":
        return {"axis": p.get("axis"), "options": list(p.get("options") or [])[:6]}
    if event_type == "node_error":
        return {
            "node_name": p.get("node_name"),
            "exception_type": p.get("exception_type"),
            "recovered": p.get("recovered"),
        }
    return {"event_type": event_type}


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> GetRecentHistoryResult:
    n = max(1, min(20, int(args.get("n") or 10)))
    event_types = list(args.get("event_types") or [])
    user_key = ctx.get("user_key")
    thread_id = ctx.get("thread_id")
    if not user_key or thread_id is None:
        return GetRecentHistoryResult(ok=True, error=None, events=[])

    # Backend probe — same pattern as conversation_log._backend_is_postgres.
    try:
        from app.main import app

        if getattr(app.state, "conv_log_backend", None) != "postgres":
            return GetRecentHistoryResult(ok=True, error=None, events=[])
    except Exception:  # noqa: BLE001
        return GetRecentHistoryResult(ok=True, error=None, events=[])

    try:
        from app.providers.db_pool import get_pool

        pool = get_pool()
        params: list[Any] = [user_key, thread_id]
        sql = "SELECT event_type, payload, created_at FROM ai.log_conversation_event WHERE user_key=%s AND thread_id=%s"
        if event_types:
            sql += " AND event_type = ANY(%s)"
            params.append(event_types)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(n)
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[tool.get_recent_history] db failed: %r", exc)
        return GetRecentHistoryResult(ok=True, error=None, events=[])

    events: list[dict[str, Any]] = []
    for row in rows or []:
        et = row[0]
        payload = row[1] if isinstance(row[1], dict) else {}
        events.append({"event_type": et, "payload_summary": _summarize_payload(et, payload)})

    return GetRecentHistoryResult(ok=True, error=None, events=events)
