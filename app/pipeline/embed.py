"""Thin re-export shim (SPEC-ARCH-AI-001 PR1).

The embed body moved verbatim into app/services/embed_service.py. The Modal
call still dispatches through this module's `EmbedProvider` name so the
monkeypatch seam (`app.pipeline.embed.EmbedProvider.embed_image_url`) used by
the characterization net and tests/test_pipeline_with_enhance.py keeps
working byte-identically.
"""

from app.observability.langfuse import observe
from app.pipeline.state import PipelineState

# Patch-seam re-export: embed_service resolves EmbedProvider via this module
# at call time, so this name must stay importable here.
from app.providers.embedding import EmbedProvider  # noqa: F401
from app.services.embed_service import embed_service

__all__ = ["embed_step", "EmbedProvider"]


@observe(name="pipeline.embed")
async def embed_step(state: PipelineState) -> PipelineState:
    return await embed_service(state)
