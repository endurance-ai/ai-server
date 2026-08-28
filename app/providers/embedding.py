import logging
import time
from typing import ClassVar

import httpx

from app.core.config import settings
from app.observability.langfuse import start_as_current_span, update_current_span
from app.providers import embedding_cache

logger = logging.getLogger(__name__)

# keepwarm 전용 고정 canary — 캐시 우회 임베드로 Modal GPU 를 warm 유지(warm()).
_WARM_CANARY = "keepwarm ping"


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
    async def warm(cls) -> int:
        """keepwarm 전용 — 실제 GPU 임베딩 경로(`/embed/text`)를 직접 호출해 Modal
        컨테이너를 warm 유지한다.

        `check_connection`(GET /health)은 GPU 임베딩 함수와 **다른 경량 핸들러**가
        응답할 수 있어, 정작 콜드 스타트하는 임베딩 경로를 못 데운다. `embed_text`
        는 PG 캐시 hit 시 Modal 을 스킵하므로 워밍에 부적합하다. 이 메서드는 고정
        canary 를 캐시 **우회**로 임베드해 매 ping 이 실제 Modal GPU 를 타게 하고,
        결과 벡터는 버린다. 반환: Modal 왕복 ms. 실패 시 예외 전파(keepwarm 루프가
        fail-open 으로 로깅)."""
        client = cls.get_client()
        _t0 = time.perf_counter()
        resp = await client.post("/embed/text", json={"text": _WARM_CANARY})
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data.get("embedding"), list) or not data["embedding"]:
            raise ValueError(f"Modal /embed/text warm unexpected response keys={list(data.keys())}")
        return int((time.perf_counter() - _t0) * 1000)

    @classmethod
    async def embed_image_url(cls, image_url: str) -> list[float]:
        """단일 이미지 URL → 768-dim 임베딩.

        Modal 측 응답 스키마: {"embedding": [float, ...], "dim": 768, "model": "..."}

        타이밍 관측 (2026-07-06 pin.it QA 트레이스 72646c8b 근거):
        `embed_text` 는 이미 modal_ms 로그 + PG 캐시가 있었지만 image 경로는
        둘 다 없었음. 그 결과 pin.it 이미지 검색이 Modal 콜드 스타트 (~19s)
        에 매번 걸리는데 트레이스에서 안 보였음. embed_text 와 동일한 로그
        포맷으로 modal_ms 를 남겨 콜드/웜 케이스를 구분 가능하게 함. 캐시는
        image_url → embedding 매핑이 텍스트만큼 결정적이지 않아 (같은 URL 도
        컨텐츠가 바뀔 수 있음) 이번 스텝에선 도입하지 않음.
        """
        client = cls.get_client()
        # 명시적 span 으로 Modal HTTP 콜을 감쌈. `pipeline.embed` @observe 는
        # 이미 있지만 `run_pipeline` 이 asyncio.gather(embed_step, enhance_query_step)
        # 로 병렬 실행하면서 OpenTelemetry context 가 끊겨 트레이스에 안 뜨는
        # 케이스가 있음 (2026-07-06 트레이스 72646c8b: pipeline.embed 부재).
        # gather 안쪽에 직접 span 붙이면 부모 컨텍스트와 무관하게 노출됨.
        with start_as_current_span("modal.embed_image"):
            _t_modal0 = time.perf_counter()
            resp = await client.post("/embed", json={"image_url": image_url})
            resp.raise_for_status()
            _modal_ms = int((time.perf_counter() - _t_modal0) * 1000)
            data = resp.json()
            embedding = data.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise ValueError(f"Modal /embed unexpected response keys={list(data.keys())}")
            update_current_span(metadata={"modal_ms": _modal_ms, "dim": len(embedding)})
        logger.info(
            "🎨 [embed_image] dim=%d · ⏱ modal=%dms url=%s",
            len(embedding),
            _modal_ms,
            image_url[:80],
        )
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
        # 명시적 span — pipeline.embed 부모 span 이 gather 컨텍스트 손실로
        # 사라지는 케이스 대비 (embed_image_url 과 같은 이유).
        with start_as_current_span("modal.embed_text"):
            _t_modal0 = time.perf_counter()
            resp = await client.post("/embed/text", json={"text": text})
            resp.raise_for_status()
            data = resp.json()
            _modal_ms = int((time.perf_counter() - _t_modal0) * 1000)
            embedding = data.get("embedding")
            if not isinstance(embedding, list) or not embedding:
                raise ValueError(f"Modal /embed/text unexpected response keys={list(data.keys())}")
            update_current_span(metadata={"modal_ms": _modal_ms, "cache_ms": _cache_ms, "dim": len(embedding)})
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
