"""SPEC-CONVERSATION-LOG-001 / plan §1.3 — shared JSON-coerce cascade.

Extracted from `session_pg._to_jsonable` so that `taste_profile_pg`,
`session_pg`, and the new `conversation_log` module all share one
implementation. Pure function, no I/O.

5-step cascade (REQ-MEMORY-SESSION-001 acceptance, preserved verbatim):

  1. `None` passthrough.
  2. Pydantic v2 `.model_dump(mode="json")` (fallback to no-arg `.model_dump()`).
  3. Dataclass instance → `dataclasses.asdict`.
  4. Recursive descent into list / tuple / dict.
  5. Primitives passthrough. Anything else → `json.loads(json.dumps(value, default=str))`.

Behavior is *byte-identical* to the pre-extraction implementation — verified by
`tests/test_jsonable.py` (DDD PRESERVE characterization).
"""

# @MX:ANCHOR: [AUTO] shared JSON cascade used by session_pg + taste_profile_pg + conversation_log
# @MX:REASON: 3 downstream callers; pure function — safe to inline at any site.
# @MX:SPEC: SPEC-CONVERSATION-LOG-001

from __future__ import annotations

import dataclasses
import json
from typing import Any

__all__ = ["to_jsonable"]


def to_jsonable(value: Any) -> Any:
    """5-step cascade per REQ-MEMORY-SESSION-001 acceptance."""
    if value is None:
        return None
    # Pydantic v2 model
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    # dataclass instance
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    # list / tuple
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    # dict
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    # primitives pass through
    if isinstance(value, (str, int, float, bool)):
        return value
    # fallback string coercion (datetime/UUID/Path/etc.)
    return json.loads(json.dumps(value, default=str))
