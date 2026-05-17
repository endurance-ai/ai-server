"""Database service (SPEC-ARCH-AI-001 PR1).

Service-facing wrapper over SupabaseProvider. PR1 introduces the seam only:
no behavior change — the class-singleton SupabaseProvider remains the
underlying implementation. Dependency injection (replacing the module/class
singleton) is PR3 (REQ-AI-003), out of scope here.

This wrapper delegates verbatim so callers can migrate to a service-shaped
API without any runtime difference.
"""

from typing import Any

from app.providers.database import SupabaseProvider


class DatabaseService:
    """Thin pass-through to SupabaseProvider (no behavior change, PR1)."""

    @staticmethod
    async def rpc(fn_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return await SupabaseProvider.rpc(fn_name, params)

    @staticmethod
    async def check_connection() -> bool:
        return await SupabaseProvider.check_connection()
