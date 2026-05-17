"""Embed service (SPEC-ARCH-AI-001 PR1).

Thin extraction of the former app/pipeline/embed.py inline body. Behavior
byte-identical (REQ-AI-007). EmbedProvider is resolved at call time via the
app.pipeline.embed module so the existing monkeypatch seam
(`app.pipeline.embed.EmbedProvider.embed_image_url`) used by the
characterization net and tests/test_pipeline_with_enhance.py keeps working.
The lazy import also avoids the shim<->service circular import.
"""

import logging

from app.pipeline.state import PipelineState

logger = logging.getLogger(__name__)


async def embed_service(state: PipelineState) -> PipelineState:
    # Lazy import: resolves the monkeypatch seam at call time and breaks the
    # app.pipeline.embed (shim) <-> app.services.embed_service import cycle.
    import app.pipeline.embed as _embed_module

    state.start("embed")
    image_url = state.request.image_url
    logger.info("[STEP 4.3][embed] Modal /embed 호출 시작 — image_url=%s", image_url)
    state.embedding = await _embed_module.EmbedProvider.embed_image_url(image_url)
    state.end("embed")
    dim = len(state.embedding) if state.embedding else 0
    head = state.embedding[:3] if state.embedding else []
    logger.info(
        "[STEP 4.4][embed] Modal /embed 응답 — dim=%d head=%s elapsed_ms=%d (cold start 포함)",
        dim,
        [f"{v:.4f}" for v in head],
        state.latency_ms.get("embed", -1),
    )
    return state
