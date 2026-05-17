"""Repository layer (SPEC-ARCH-AI-001 PR2, REQ-AI-002).

The search RPC name + parameter mapping live here in exactly one place.
"""

from app.infrastructure.repositories.search_repository import SearchRepository

__all__ = ["SearchRepository"]
