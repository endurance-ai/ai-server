"""Shared dict/object attribute reader for re-hydrated `last_results`.

last_results is round-tripped through JSONB by SPEC-MEMORY-001 — re-hydration
normally re-wraps each entry as `SimpleNamespace` (see session_pg.py
`_rehydrate_candidates`), but downstream code stays defensive for either shape.
"""

from __future__ import annotations

from typing import Any


def attr(c: Any, key: str, default: Any = None) -> Any:
    """Read `key` from a candidate that may be a dict or an object."""
    if isinstance(c, dict):
        return c.get(key, default)
    return getattr(c, key, default)
