"""ContextVar-bound channel adapter accessor for graph nodes.

The webhook handler binds the active `MessengerAdapter` before invoking the
graph. Nodes that need to send text / cards (`pick_item`, `ask_clarify`,
`send_results`, `respond`) read it through `get_adapter()`.

This mirrors how `app/channels/session.py` exposes the SessionStore — keeps
graph state lean (REQ-STATE-005) by NOT carrying the adapter inside
WorkingState.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.channels.adapter import MessengerAdapter


_adapter_var: ContextVar[MessengerAdapter | None] = ContextVar("graph_adapter", default=None)


def set_adapter(adapter: MessengerAdapter | None) -> object:
    """Bind an adapter for the current task scope. Returns the token used to
    reset (`reset_adapter(token)`)."""
    return _adapter_var.set(adapter)


def reset_adapter(token: object) -> None:
    _adapter_var.reset(token)  # type: ignore[arg-type]


def get_adapter() -> MessengerAdapter:
    """Return the bound adapter or raise a clear error (R9 mitigation)."""
    a = _adapter_var.get()
    if a is None:
        raise RuntimeError(
            "graph adapter is not bound; the webhook handler must call set_adapter(...) before invoking the graph"
        )
    return a
