from app.observability.langfuse import observe
from app.pipeline.state import PipelineState
from app.providers.embedding import EmbedProvider


@observe(name="pipeline.embed")
async def embed_step(state: PipelineState) -> PipelineState:
    state.start("embed")
    state.embedding = await EmbedProvider.embed_image_url(state.request.image_url)
    state.end("embed")
    return state
