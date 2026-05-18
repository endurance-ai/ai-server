"""Canonical category-family normalization (SPEC-SEARCH-V6-001 family-gate).

`search_products_v6` FILTER 2 is a canonical FAMILY gate. The RPC internally
does `lower(trim(p_category))` + a `category_canonical` lookup that resolves
the input to one of 20 canonical families (19 apparel + an `other`
catch-all). The gate engages ONLY when `p_category` is exactly one of those
20 lowercase tokens (every token is live as an identity row). ANY other
string — a Vision raw value, "Earrings", "악세사리", a brand style-node letter
— resolves to `other`, the family gate is skipped, and the RPC degrades
gracefully to cosine-only ranking (NOT broken; intentional).

DESIGN — call-site 2-layer map, NOT a prompt constraint:
  `app/channels/vision_prompt.py` is the SPEC-VISION-UNIFY-001 byte-verbatim
  mirror of kikoai/app `analyze.ts` with active drift-detection. It MUST NOT
  be edited. All 20-token normalization therefore happens HERE, at the search
  call site (sibling of `search_rpc_contract.py`, matching the repo pattern),
  and is plumbed into `SearchRepository.build_params`.

`to_canonical_family` is pure (no I/O), total (NEVER returns None — always
exactly one of the 20 lowercase tokens), and fully unit-testable.
"""

from __future__ import annotations

# The 20 canonical families (lowercase, verbatim). These are the ONLY allowed
# `p_category` tokens that engage the v6 family gate. `other` is the official
# catch-all — never invent a token outside this set.
CANONICAL_FAMILIES: frozenset[str] = frozenset(
    {
        "tops",
        "bottoms",
        "dresses",
        "outerwear",
        "knitwear",
        "shoes",
        "sneakers",
        "bags",
        "accessories",
        "eyewear",
        "jewelry",
        "headwear",
        "underwear",
        "swimwear",
        "activewear",
        "homeware",
        "fragrance",
        "beauty",
        "lifestyle",
        "other",
    }
)

# Bot Vision 7-enum (vision_prompt.py:150 — `Outer/Top/Bottom/Shoes/Bag/Dress/
# Accessories`) lowercased → canonical family. Plus a SMALL, conservative set
# of obvious singular/plural/synonym defensives whose target is one of the 20.
# When unsure → DO NOT add an entry; let it fall through to `other`.
#
# shoes vs sneakers: bot Vision emits only "Shoes" (no sneaker split), so
# `shoes→shoes` is INTENTIONAL coarseness — the `sneakers` family is
# unreachable from the bot Vision path; the gate still engages on `shoes`.
# This is by design, NOT a bug.
_VISION_ALIAS: dict[str, str] = {
    # Vision 7-enum (the primary, authoritative mapping)
    "outer": "outerwear",
    "top": "tops",
    "bottom": "bottoms",
    "shoes": "shoes",
    "bag": "bags",
    "dress": "dresses",
    "accessories": "accessories",
    # Conservative defensives — each target ∈ the 20 and trivially unambiguous
    "jewellery": "jewelry",
    "glasses": "eyewear",
    "sunglasses": "eyewear",
    "hat": "headwear",
    "cap": "headwear",
}


def to_canonical_family(raw: str | None) -> str:
    """Normalize an arbitrary category string to a canonical v6 family token.

    Resolution order:
      1. identity passthrough — `t in CANONICAL_FAMILIES` → return `t`
         (supports the /recommend path where the app may already send a
         20-token, and any future Vision canonical output);
      2. Vision-alias map — `t in _VISION_ALIAS` → return the mapped family;
      3. else → `"other"` (the v6 catch-all → family gate intentionally
         skipped → graceful cosine-only degrade).

    Always returns exactly ONE lowercase token from `CANONICAL_FAMILIES`;
    NEVER returns None (the v6 contract requires `p_category` to be exactly
    one lowercase token).
    """
    t = (raw or "").strip().lower()
    if t in CANONICAL_FAMILIES:
        return t
    if t in _VISION_ALIAS:
        return _VISION_ALIAS[t]
    return "other"


__all__ = [
    "CANONICAL_FAMILIES",
    "to_canonical_family",
]
