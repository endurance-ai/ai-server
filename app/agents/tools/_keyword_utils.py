"""Shared keyword helpers — used by `search_products` and `refine_search`.

Both tools accept `boost_keywords` / `exclude_keywords` from the LLM and need
to (a) defensively cast them to a clean list[str] and (b) join boost tokens
into the text_query without duplicating tokens already present (the B15
fix: chained refines no longer accumulate "roomy roomy roomy").

Lives in its own module so `search_products` and `refine_search` can both
import without creating a cycle (refine_search already imports from
search_products).
"""

from __future__ import annotations


def as_keyword_list(v: object) -> list[str]:
    """Defensive cast for `boost_keywords` / `exclude_keywords` args.

    `validate_args` already rejects non-list inputs at the agent boundary,
    but if anything slips through (test monkeypatch, future tool added with
    a different shape), naive `list(some_string)` explodes a single keyword
    string into per-character tokens (["t","-","s","h","i","r","t"]) that
    then contaminate the embedded query. This belt-and-suspenders cast
    keeps the embed input well-formed regardless of upstream validation
    state.

    Mapping:
      None / empty / whitespace-only → []
      single string                  → [string.strip()]
      list/tuple                     → [str(x) for each truthy x]
      anything else                  → []
    """
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x]
    return []


def dedup_join(base_query: str, boost: list[str]) -> str:
    """Join `base_query` + `boost` keywords with case-insensitive token
    dedup. Order from base_query is preserved (it's the canonical product
    query); new boost tokens are appended only when not already present.
    Whitespace-normalised.

    Examples:
      base="wide jeans women roomy", boost=["roomy"]
        → "wide jeans women roomy"           (no dup)
      base="black blazer", boost=["cropped", "side-button"]
        → "black blazer cropped side-button"
      base="", boost=["red", "dress"]
        → "red dress"
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in (base_query or "").split():
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    for kw in boost:
        for tok in str(kw).split():
            key = tok.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(tok)
    return " ".join(out).strip()
