"""SPEC-GENDER-PIN-001 (260522) — pending gender-gated search store.

When a text search arrives with no resolvable gender (no per-request gender
word, no pinned `taste_profile.gender`), `search_products` sends a one-time
gender card and stashes the original search args here keyed by chat_id. The
`clarify:gender:{value}` callback (handled inline in `ingest`) pins the chosen
gender to the taste profile, pops these args, and re-runs the search
deterministically with the gender applied — so the user sees results in the
same flow without re-typing.

In-process module state (mirrors `pending_question.py`): NOT persisted across
restarts on purpose — a dangling gender card after a restart simply re-asks,
which is harmless. Best-effort; tolerates invalid chat_id by no-op'ing.
"""

from __future__ import annotations

from typing import Any

# chat_id -> stored search args dict (text_query/category/fit/color/top_k).
_PENDING: dict[int, dict[str, Any]] = {}


def _coerce_chat_id(chat_id: Any) -> int | None:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None


def set_pending(chat_id: Any, args: dict[str, Any]) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None or not isinstance(args, dict):
        return
    _PENDING[cid] = dict(args)


def pop_pending(chat_id: Any) -> dict[str, Any] | None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return None
    return _PENDING.pop(cid, None)


def clear_pending(chat_id: Any) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    _PENDING.pop(cid, None)


def _reset_all_for_tests() -> None:
    _PENDING.clear()
