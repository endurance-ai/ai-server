import logging
import time
from typing import ClassVar

import httpx

from app.core.config import settings
from app.providers import embedding_cache

logger = logging.getLogger(__name__)


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
    async def embed_text(cls, text: str) -> list[float]:
        """단일 텍스트 쿼리 → 768-dim 임베딩 (SPEC-SEARCH-V6-001).

        Modal /embed/text 엔드포인트는 동일한 FashionSigLIP L2 공간을 노출하므로
        이미지 임베딩과 cross-modal cosine 비교가 유효하다 (v6 embedding-first).
        Modal 측 응답 스키마: {"embedding": [float, ...], "dim": 768}

        PG 캐시 (ai.embedding_cache_text) 우선 조회 — hit 시 Modal 호출 스킵.
        모델이 deterministic 이므로 동일 (model_ver, normalize_ver, text) → 동일
        벡터 보장. 캐시 lookup/put 실패는 fail-open (Modal 본 경로로 계속).
        """
        # 260522 timing — separate cache-lookup time from Modal HTTP time so a
        # slow text search is attributable (cache hit ≈ 0ms; miss = Modal call,
        # cold-start prone). Live trace: a cold Modal /embed/text took ~19s and
        # tripped the agent's per-tool timeout → retry → ~29s total.
        _t_cache0 = time.perf_counter()
        cached = await embedding_cache.get_cached(text)
        _cache_ms = int((time.perf_counter() - _t_cache0) * 1000)
        if cached is not None:
            logger.info(
                "🗃️  [embed_cache] hit text='%s' dim=%d · ⏱ lookup=%dms",
                text[:60],
                len(cached),
                _cache_ms,
            )
            return cached

        client = cls.get_client()
        _t_modal0 = time.perf_counter()
        resp = await client.post("/embed/text", json={"text": text})
        resp.raise_for_status()
        data = resp.json()
        _modal_ms = int((time.perf_counter() - _t_modal0) * 1000)
        embedding = data.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ValueError(f"Modal /embed/text unexpected response keys={list(data.keys())}")
        logger.info(
            "🗃️  [embed_cache] miss → put text='%s' dim=%d · ⏱ lookup=%dms modal=%dms",
            text[:60],
            len(embedding),
            _cache_ms,
            _modal_ms,
        )
        await embedding_cache.put_cached(text, embedding)
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
