"""Thin re-export shim (SPEC-ARCH-AI-001 PR1).

The search orchestration + query-text selection moved verbatim into
app/services/search_service.py. The actual RPC call still dispatches through
this module's `DatabaseProvider` name so the monkeypatch seam
(`app.pipeline.search.DatabaseProvider.rpc`) used by the characterization net
and tests/test_pipeline_with_enhance.py keeps working byte-identically.
"""

from app.observability.langfuse import observe
from app.pipeline.state import PipelineState

# Patch-seam re-export: search_service resolves DatabaseProvider via this
# module at call time, so this name must stay importable here.
from app.providers.database import DatabaseProvider  # noqa: F401
from app.services.search_service import embedding_to_pgvector as _embedding_to_pgvector
from app.services.search_service import search_service

__all__ = ["search_step", "_embedding_to_pgvector", "DatabaseProvider"]


@observe(name="pipeline.search")
async def search_step(state: PipelineState) -> PipelineState:
    return await search_service(state)
