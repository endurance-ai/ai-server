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
from app.core.config import model_supports_prompt_caching, settings

logger = logging.getLogger(__name__)

# model-tiering: 모델 ID → tool-bound 클라이언트 캐시(모델별 싱글톤). 기존
# 단일 _llm 대신 dict 로 관리해 라우팅=Sonnet / follow-through=Haiku 를 동시에 둔다.
_llm_by_model: dict[str, Any] = {}


def _build_tools_schema() -> list[dict[str, Any]]:
    """Build OpenAI Tools schema from REGISTRY.

    Pydantic v2 TypedDict → JSON Schema is automatic via langchain helpers but
    we emit a minimal hand-rolled schema to avoid forcing a deep langchain dep
    here. Each tool's args TypedDict annotations become properties.
    """
    tools: list[dict[str, Any]] = []
    for name, meta in REGISTRY.items():
        # web_search is only advertised to the LLM when a search key is set —
        # otherwise the model would call a dead tool. The dispatch also guards.
        if name == "web_search" and not (settings.TAVILY_API_KEY or "").strip():
            continue
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
    # cache_control on the last tool signals Anthropic to cache the entire tools
    # block (all definitions are stable across iterations). LiteLLM forwards this
    # field unchanged — Anthropic caches, but non-Anthropic Bedrock models
    # (Kimi/Moonshot, Qwen, Nova) 500 on it, so only emit it for Claude.
    if tools and model_supports_prompt_caching(settings.AGENT_LLM_MODEL):
        tools[-1]["cache_control"] = {"type": "ephemeral"}
    return tools


def get_llm(model: str | None = None) -> Any:
    """Return a tool-bound ChatOpenAI client (모델별 싱글톤). None when fail-closed.

    `model` 미지정이면 AGENT_LLM_MODEL(기본 두뇌). model-tiering 에서 라우팅
    iteration 은 AGENT_ROUTER_LLM_MODEL 로 이 함수를 호출한다."""
    resolved = (model or settings.AGENT_LLM_MODEL or "").strip()
    if not resolved:
        logger.warning("[agent_v2] AGENT_LLM_MODEL not configured — fail-closed")
        return None
    cached = _llm_by_model.get(resolved)
    if cached is not None:
        return cached
    try:
        from app.providers.litellm_chat import LiteLLMChatOpenAI

        api_key = settings.LITELLM_MASTER_KEY or "missing-litellm-master-key"
        client = LiteLLMChatOpenAI(
            model=resolved,
            base_url=settings.LITELLM_BASE_URL + "/v1",
            api_key=api_key,
            temperature=0.4,
            timeout=max(0.1, settings.AGENT_LLM_TIMEOUT_S),
        )
        # Explicit `tool_choice=None` → langchain MUST NOT inject a tool_choice
        # field into the request body. The ReAct loop relies on the model
        # autonomously choosing tools or terminating with `respond`; omitting
        # tool_choice is functionally "auto" for OpenAI AND required for Bedrock.
        bound = client.bind_tools(_build_tools_schema(), tool_choice=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent_v2] LLM bind failed (model=%s): %r", resolved, exc)
        return None
    _llm_by_model[resolved] = bound
    return bound
