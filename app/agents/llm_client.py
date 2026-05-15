"""SPEC-AGENT-V2-REACT / T-004 — ChatOpenAI client wrapper for the ReAct loop.

Wraps `langchain_openai.ChatOpenAI` via the existing LiteLLM proxy. Lazy
singleton — created on first invoke. Built with `bind_tools(...)` so the
OpenAI Tools API produces structured tool calls.

@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any

from app.agents.tool_registry import REGISTRY
from app.core.config import settings

logger = logging.getLogger(__name__)

_llm: Any = None


def _build_tools_schema() -> list[dict[str, Any]]:
    """Build OpenAI Tools schema from REGISTRY.

    Pydantic v2 TypedDict → JSON Schema is automatic via langchain helpers but
    we emit a minimal hand-rolled schema to avoid forcing a deep langchain dep
    here. Each tool's args TypedDict annotations become properties.
    """
    tools: list[dict[str, Any]] = []
    for name, meta in REGISTRY.items():
        td = meta["args_typeddict"]
        annotations = getattr(td, "__annotations__", {})
        properties: dict[str, Any] = {}
        for key in annotations:
            # Loose schema — relies on OpenAI Tools API's flexible coercion.
            properties[key] = {"type": "string"}
        required = list(getattr(td, "__required_keys__", []))
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": meta["description"],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return tools


def get_llm() -> Any:
    """Return a tool-bound ChatOpenAI singleton. Returns None when fail-closed."""
    global _llm
    if _llm is not None:
        return _llm
    model = (settings.AGENT_LLM_MODEL or "").strip()
    if not model:
        logger.warning("[agent_v2] AGENT_LLM_MODEL not configured — fail-closed")
        return None
    try:
        from langchain_openai import ChatOpenAI

        api_key = settings.LITELLM_MASTER_KEY or "missing-litellm-master-key"
        client = ChatOpenAI(
            model=model,
            base_url=settings.LITELLM_BASE_URL + "/v1",
            api_key=api_key,
            temperature=0.4,
            timeout=max(0.1, settings.AGENT_LLM_TIMEOUT_S),
        )
        _llm = client.bind_tools(_build_tools_schema())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent_v2] LLM bind failed: %r", exc)
        return None
    return _llm
