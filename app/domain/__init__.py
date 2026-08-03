"""Domain layer (SPEC-ARCH-AI-001 PR5, REQ-AI-005).

Internal business models, separate from the Pydantic transport DTOs in
app/models/. This boundary lets SPEC-SEARCH-UNIFY-001's v6 evolve internal
search types without perturbing the /recommend wire schema. Additive only —
the runner DTO-serialization path is byte-identical (Net(1) unchanged).
"""

from app.domain.search import SearchCandidate, search_candidate_from_row

__all__ = ["SearchCandidate", "search_candidate_from_row"]
