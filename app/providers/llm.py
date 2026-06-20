from typing import Any, ClassVar

import httpx

from app.core.config import settings


class LLMProvider:
    """LiteLLM 프록시 클라이언트 (httpx async).

    Vision은 kikoai/app(Next.js)이 담당. 여기는 enhance_query/rerank 등 텍스트 LLM 용도.
    """

    _client: ClassVar[httpx.AsyncClient | None] = None

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            headers = {}
            if settings.LITELLM_MASTER_KEY:
                headers["Authorization"] = f"Bearer {settings.LITELLM_MASTER_KEY}"
            cls._client = httpx.AsyncClient(
                base_url=settings.LITELLM_BASE_URL,
                timeout=60.0,
                headers=headers,
            )
        return cls._client

    @classmethod
    async def check_connection(cls) -> bool:
        try:
            client = cls.get_client()
            resp = await client.get("/health/liveliness", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    @classmethod
    async def chat(
        cls,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: int = 1000,
        *,
        source: str = "unknown",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """OpenAI-호환 chat completion.

        ``source`` labels the call-site (vision / evaluator / router / ...) for
        the per-source cost breakdown in turn_cost.
        """
        client = cls.get_client()
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                **kwargs,
            },
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        try:
            from app.observability.turn_cost import accumulate_raw

            # Prefer the actually-routed model from the response (LiteLLM may map
            # an alias to a different model) — fall back to the requested name.
            actual_model = data.get("model") or model
            accumulate_raw(actual_model, data.get("usage") or {}, source=source)
        except Exception:  # noqa: BLE001 — observability must never block
            pass
        return data

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
