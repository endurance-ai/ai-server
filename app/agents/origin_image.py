"""Origin image store for multi-turn vector blending.

When a user sends a fashion image, Vision analysis converts it to text
attributes (searchQuery). But the raw image embedding carries richer visual
information -- silhouette, texture, colour nuance -- that is lost when only
the text-to-embedding path is used.

This store saves:
  - the original image URL (set immediately after Vision, no extra API call)
  - the 768-dim FashionSigLIP vector (lazily embedded on the first refine turn
    and cached for subsequent refines)

On follow-up turns ("more casual", "different colour"), `refine_search`:
  1. Checks for a stored image URL / vector.
  2. Embeds the image URL on first access (one Modal call per image session).
  3. Blends: query_vec = normalize(alpha * origin_vec + (1-alpha) * modifier_vec)
  4. Uses blended vector for pgvector search.

Lifecycle: same as `last_query.py` -- ephemeral, single process, dropped on
restart (harmless: refine falls back to text-only path on cache miss).
"""

from __future__ import annotations

import math
from typing import Any

# chat_id -> (image_url, optional_embedding_vector)
# @MX:WARN: [AUTO] process-local + unbounded; written once per image turn.
# @MX:REASON: single-worker assumption. At scale move to Redis with TTL 24h.
_STORE: dict[int, tuple[str, list[float] | None]] = {}


def _coerce_chat_id(chat_id: Any) -> int | None:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None


def set_origin_url(chat_id: Any, image_url: str) -> None:
    """Store image URL immediately after Vision (no Modal call)."""
    cid = _coerce_chat_id(chat_id)
    if cid is None or not image_url:
        return
    _STORE[cid] = (image_url, None)


def set_origin_vector(chat_id: Any, vector: list[float]) -> None:
    """Cache the lazily-computed embedding vector for subsequent refines."""
    cid = _coerce_chat_id(chat_id)
    if cid is None or not vector:
        return
    existing = _STORE.get(cid)
    url = existing[0] if existing else ""
    if url:
        _STORE[cid] = (url, list(vector))


def get_origin_url(chat_id: Any) -> str | None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return None
    entry = _STORE.get(cid)
    return entry[0] if entry else None


def get_origin_vector(chat_id: Any) -> list[float] | None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return None
    entry = _STORE.get(cid)
    return entry[1] if entry else None


def clear_origin(chat_id: Any) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    _STORE.pop(cid, None)


def blend_vectors(origin: list[float], modifier: list[float], alpha: float = 0.7) -> list[float]:
    """Weighted blend of two L2-normalised vectors, re-normalised.

    alpha=0.7 keeps 70 % of the original image signal and 30 % of the text
    modifier direction -- enough to steer colour/mood without losing the
    original outfit identity.
    """
    blended = [alpha * o + (1.0 - alpha) * m for o, m in zip(origin, modifier)]
    norm = math.sqrt(sum(v * v for v in blended)) or 1.0
    return [v / norm for v in blended]


def vector_arithmetic(
    origin: list[float],
    subtract: list[float],
    add: list[float],
    *,
    beta: float = 0.5,
) -> list[float]:
    """Explicit attribute swap via vector arithmetic (Level 2 advanced).

    Formula:
        result = normalize(origin - beta * subtract + beta * add)

    Rationale (multi-turn eval 2026-06-06):
      Plain weighted-sum blending leaks too much of the original attribute
      (e.g. "grey") into the blended vector, so a follow-up like "make it blue"
      gets drowned out (modifier_score ~0.06 at alpha=0.7). Vector arithmetic
      lets the caller explicitly subtract the OLD attribute and add the NEW
      one, which is what FashionSigLIP's CLIP-style joint image+text space is
      designed for. This is the standard analogy-style edit, e.g.
      `image - "grey" + "blue navy"` keeps the silhouette and swaps only the
      colour token.

    Parameters:
        origin   -- L2-normalised query vector (image or its text stand-in).
        subtract -- L2-normalised vector for the attribute being removed.
        add      -- L2-normalised vector for the attribute being added.
        beta     -- swap strength (0.5 is the eval starting point — strong
                    enough to flip a single colour/fit token without erasing
                    the rest of the outfit).
    """
    result = [o - beta * s + beta * a for o, s, a in zip(origin, subtract, add)]
    norm = math.sqrt(sum(v * v for v in result)) or 1.0
    return [v / norm for v in result]


def _reset_all_for_tests() -> None:
    _STORE.clear()
