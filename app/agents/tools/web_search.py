"""`web_search` tool — live web lookup via the Tavily search API.

Bedrock (our LLM route) does NOT offer Claude's built-in server-side web_search
tool, so we wire an external search API as a normal ReAct tool. The agent uses
it to decode style references it can't answer from the catalog or its own
knowledge — '닝닝 공항패션st', an unfamiliar brand's aesthetic — then translates
the snippets into a concrete `search_products` query.

Fail-open: any missing key / timeout / HTTP / parse error returns
``ok=True, results=[]`` (never raises) so the agent degrades to a normal search
instead of dead-ending the turn. Exposure is gated in
``llm_client._build_tools_schema`` on ``settings.TAVILY_API_KEY`` — this dispatch
is a second guard.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.agents.tool_registry import WebSearchResult
from app.core.config import settings

logger = logging.getLogger(__name__)

# Trim snippet bodies so a few results don't blow up the agent's context.
_SNIPPET_MAX = 500
_ANSWER_MAX = 700


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> WebSearchResult:
    query = (args.get("query") or "").strip()
    if not query:
        return WebSearchResult(ok=False, error="empty_query", answer=None, results=[])

    api_key = (settings.TAVILY_API_KEY or "").strip()
    if not api_key:
        # Should not happen (tool is gated on the key), but never dead-end.
        logger.info("[tool.web_search] no TAVILY_API_KEY — skipped (fail-open)")
        return WebSearchResult(ok=True, error="disabled", answer=None, results=[])

    timeout_s = max(1.0, settings.WEB_SEARCH_TIMEOUT_MS / 1000.0)
    max_results = max(1, int(settings.WEB_SEARCH_MAX_RESULTS or 5))
    payload = {
        "api_key": api_key,
        "query": query[:400],
        # `basic` depth is fast + cheap; `include_answer` gives a synthesized
        # summary the agent can read directly. No raw HTML.
        "search_depth": "basic",
        "include_answer": True,
        "max_results": max_results,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(f"{settings.TAVILY_BASE_URL.rstrip('/')}/search", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 — web search is auxiliary, never break the turn
        logger.warning("[tool.web_search] failed q=%r: %s", query[:80], type(exc).__name__)
        return WebSearchResult(ok=True, error=f"search_failed:{type(exc).__name__}", answer=None, results=[])

    answer = data.get("answer")
    answer = answer[:_ANSWER_MAX] if isinstance(answer, str) else None
    results: list[dict[str, Any]] = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        results.append(
            {
                "title": str(r.get("title") or "")[:160],
                "url": str(r.get("url") or "")[:300],
                "content": str(r.get("content") or "")[:_SNIPPET_MAX],
            }
        )
        if len(results) >= max_results:
            break

    logger.info("🌐 [tool.web_search] q=%r → answer=%s results=%d", query[:80], bool(answer), len(results))
    return WebSearchResult(ok=True, error=None, answer=answer, results=results)
