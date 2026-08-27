"""ChatOpenAI adapter that preserves LiteLLM billing metadata.

LangChain normalizes OpenAI-compatible usage into ``usage_metadata`` but drops
provider-specific fields such as Anthropic cache-write tokens. LiteLLM also
returns the authoritative per-request spend in the
``x-litellm-response-cost`` header. This adapter keeps both on the resulting
``AIMessage.response_metadata`` so the conversation cost ledger can record the
same amount as the gateway spend log.
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI


class LiteLLMChatOpenAI(ChatOpenAI):
    """ChatOpenAI with response headers and unmodified usage attached."""

    include_response_headers: bool = True

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict[str, Any] | None = None,
    ) -> Any:
        response_dict = response if isinstance(response, dict) else response.model_dump()
        result = super()._create_chat_result(response, generation_info)
        raw_usage = response_dict.get("usage")
        response_cost = response_dict.get("response_cost")
        hidden = response_dict.get("_hidden_params") or {}
        if response_cost is None and isinstance(hidden, dict):
            response_cost = hidden.get("response_cost") or hidden.get("cost")

        for generation in result.generations:
            message = generation.message
            if generation_info:
                message.response_metadata.update(generation_info)
            if raw_usage:
                message.response_metadata["raw_usage"] = raw_usage
            if response_cost is not None:
                message.response_metadata["response_cost"] = response_cost
        return result
