"""Database service -- the designated DI seam over SupabaseProvider.

SPEC-ARCH-AI-001 PR1 introduced this seam; review P1-b WIRED it:
``SearchRepository.search`` now dispatches its ``search_products_v6`` RPC
through ``DatabaseService.rpc`` instead of calling ``SupabaseProvider`` (via
the pipeline module) directly, so this is the real RPC chokepoint and no
longer dead code.

Behavior is byte-identical: ``DatabaseService.rpc`` delegates verbatim and
resolves the ``SupabaseProvider`` CLASS attribute at call time, so the
characterization monkeypatch seam
(``app.pipeline.search.SupabaseProvider.rpc``), which mutates that same
shared class object, keeps intercepting unchanged (Net(3) param snapshot is
preserved 0-diff).

Full dependency injection (replacing the class singleton with an injected
instance) remains PR3 (REQ-AI-003), out of scope here -- this class stays the
designated DI seam for that future SPEC.
"""

from typing import Any

from app.providers.database import SupabaseProvider


class DatabaseService:
    """Designated DI seam over SupabaseProvider (wired -- review P1-b).

    Thin pass-through (no behavior change): every method delegates verbatim
    to the ``SupabaseProvider`` class, resolved at call time so the
    characterization monkeypatch seam stays effective. This is the single
    RPC chokepoint ``SearchRepository.search`` routes through; full DI
    instance injection is the future PR3 (REQ-AI-003) responsibility.
    """

    @staticmethod
    async def rpc(fn_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return await SupabaseProvider.rpc(fn_name, params)

    @staticmethod
    async def check_connection() -> bool:
        return await SupabaseProvider.check_connection()
