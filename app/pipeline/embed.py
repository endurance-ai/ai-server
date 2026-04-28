import logging

from app.observability.langfuse import observe
from app.pipeline.state import PipelineState
from app.providers.embedding import EmbedProvider

logger = logging.getLogger(__name__)


@observe(name="pipeline.embed")
async def embed_step(state: PipelineState) -> PipelineState:
    state.start("embed")
    image_url = state.request.image_url
    logger.info("[STEP 4.3][embed] Modal /embed 호출 시작 — image_url=%s", image_url)
    state.embedding = await EmbedProvider.embed_image_url(image_url)
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
