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
        **kwargs: Any,
    ) -> dict[str, Any]:
        """OpenAI-호환 chat completion."""
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
        return resp.json()

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
