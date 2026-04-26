from typing import ClassVar

import httpx

from app.core.config import settings


class EmbedProvider:
    """Modal /embed 엔드포인트 클라이언트 (FashionSigLIP)."""

    _client: ClassVar[httpx.AsyncClient | None] = None

    @classmethod
    def get_client(cls) -> httpx.AsyncClient:
        if cls._client is None:
            headers = {}
            if settings.MODAL_EMBED_TOKEN:
                headers["Authorization"] = f"Bearer {settings.MODAL_EMBED_TOKEN}"
            cls._client = httpx.AsyncClient(
                base_url=settings.MODAL_EMBED_URL,
                timeout=settings.MODAL_EMBED_TIMEOUT,
                headers=headers,
            )
        return cls._client

    @classmethod
    async def check_connection(cls) -> bool:
        try:
            client = cls.get_client()
            resp = await client.get("/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    @classmethod
    async def embed_image_url(cls, image_url: str) -> list[float]:
        """단일 이미지 URL → 768-dim 임베딩.

        Modal 측 응답 스키마: {"embedding": [float, ...], "dim": 768, "model": "..."}
        """
        client = cls.get_client()
        resp = await client.post("/embed", json={"image_url": image_url})
        resp.raise_for_status()
        data = resp.json()
        embedding = data.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ValueError(f"Modal /embed unexpected response keys={list(data.keys())}")
        return embedding

    @classmethod
    async def embed_image_urls(cls, image_urls: list[str]) -> list[list[float]]:
        """여러 이미지 URL → 임베딩 리스트 (배치)."""
        client = cls.get_client()
        resp = await client.post("/embed/batch", json={"image_urls": image_urls})
        resp.raise_for_status()
        data = resp.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
            raise ValueError(f"Modal /embed/batch unexpected response keys={list(data.keys())}")
        return embeddings

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            await cls._client.aclose()
            cls._client = None
