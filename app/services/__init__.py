"""Service layer (SPEC-ARCH-AI-001 PR1).

Business logic extracted verbatim from app/pipeline/ steps. The original
app/pipeline/ modules remain as thin re-export shims so all existing import
sites and characterization tests keep working unchanged.

Behavior is byte-identical to the pre-extraction inline code (REQ-AI-007).
"""

from app.services.database_service import DatabaseService
from app.services.diversify_service import diversify_service, tolerance_to_target_count
from app.services.embed_service import embed_service
from app.services.search_service import embedding_to_pgvector, search_service

__all__ = [
    "DatabaseService",
    "diversify_service",
    "tolerance_to_target_count",
    "embed_service",
    "search_service",
    "embedding_to_pgvector",
]
