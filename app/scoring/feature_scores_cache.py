"""Per-user visual-feature taste scores, cached for the search re-rank.

``ai.user_feature_scores`` is keyed by the app's auth UUID, but ``search_service``
only ever sees the derived ``user_key`` (``c:{synthetic_chat_id}``).
``chat_service`` — the one place that holds the UUID — loads the decayed scores
once per app turn and stashes them here; ``search_service`` reads them back by
``user_key``. The internal ``/recommend`` path never populates
this cache, so their search stays byte-for-byte unchanged.

In-memory + TTL, mirroring the existing in-process ``TasteProfileStore``. A miss
is harmless (search simply falls back to the non-feature order), so eviction can
never break a request.
"""

from __future__ import annotations

import time

_TTL_S = 900.0
_store: dict[str, tuple[float, dict[tuple[str, str], float]]] = {}


def put(user_key: str, scores: dict[tuple[str, str], float]) -> None:
    _store[user_key] = (time.monotonic() + _TTL_S, scores)


def get(user_key: str) -> dict[tuple[str, str], float] | None:
    entry = _store.get(user_key)
    if entry is None:
        return None
    expires, scores = entry
    if time.monotonic() > expires:
        _store.pop(user_key, None)
        return None
    return scores


def clear() -> None:
    """Test seam — drop all cached scores."""
    _store.clear()
