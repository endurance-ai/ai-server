"""Database service -- the designated DI seam over DatabaseProvider.

SPEC-ARCH-AI-001 PR1 introduced this seam; review P1-b WIRED it:
``SearchRepository.search`` now dispatches its ``search_products_v6`` RPC
through ``DatabaseService.rpc`` instead of calling ``DatabaseProvider`` (via
the pipeline module) directly, so this is the real RPC chokepoint and no
longer dead code.

Behavior is byte-identical: ``DatabaseService.rpc`` delegates verbatim and
resolves the ``DatabaseProvider`` CLASS attribute at call time, so the
characterization monkeypatch seam
(``app.pipeline.search.DatabaseProvider.rpc``), which mutates that same
shared class object, keeps intercepting unchanged (Net(3) param snapshot is
preserved 0-diff).

Full dependency injection (replacing the class singleton with an injected
instance) remains PR3 (REQ-AI-003), out of scope here -- this class stays the
designated DI seam for that future SPEC.
"""

from typing import Any

from app.providers.database import DatabaseProvider


class DatabaseService:
    """Designated DI seam over DatabaseProvider (wired -- review P1-b).

    Thin pass-through (no behavior change): every method delegates verbatim
    to the ``DatabaseProvider`` class, resolved at call time so the
    characterization monkeypatch seam stays effective. This is the single
    RPC chokepoint ``SearchRepository.search`` routes through; full DI
    instance injection is the future PR3 (REQ-AI-003) responsibility.
    """

    @staticmethod
    async def rpc(fn_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return await DatabaseProvider.rpc(fn_name, params)

    @staticmethod
    async def check_connection() -> bool:
        return await DatabaseProvider.check_connection()
