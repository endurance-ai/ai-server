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

# 2026-07-15 백엔드 정규화 반영: products.category 는 이제 14 family + other
# 로 정규화 완료 (실 DB 확인 — sneakers 카테고리 상품 0행). `sneakers` family
# 로 게이트를 걸면 rung 2 count=0 → rung 3 로 떨어져 family gate 가 통째로
# 무력화되므로, sneakers 로 resolve 되는 모든 입력은 `shoes` 로 리맵한다.
# 스니커즈 정밀도는 subcategory 레벨(`p_subcategory='sneakers'`, DB 2.3k행)
# 에서 해결된다 — subcategory_vocab.py 참조.
_FAMILY_REMAP: dict[str, str] = {
    "sneakers": "shoes",
}

# Bot Vision 7-enum (vision_prompt.py:150 — `Outer/Top/Bottom/Shoes/Bag/Dress/
# Accessories`) lowercased → canonical family. Plus a SMALL, conservative set
# of obvious singular/plural/synonym defensives whose target is one of the 20.
# When unsure → DO NOT add an entry; let it fall through to `other`.
_VISION_ALIAS: dict[str, str] = {
    # Vision 7-enum (the primary, authoritative mapping)
    "outer": "outerwear",
    "top": "tops",
    "bottom": "bottoms",
    "shoes": "shoes",
    "bag": "bags",
    "dress": "dresses",
    "accessories": "accessories",
    # 260611 — LLM `category` arg is free-form English (from prompt: "pass a
    # concise ENGLISH text_query / category" — search_products.py). Without
    # these aliases nearly every search resolves to `other` and the family
    # gate is skipped, so ranking degrades to cosine-only and a "모자" query
    # can match sweaters. Each entry targets exactly one of the 20 canonical
    # families. When unsure (e.g. ambiguous "scarf" → could be accessories
    # OR knitwear) leave it out and let `other` engage.
    # tops
    "shirt": "tops",
    "shirts": "tops",
    "tshirt": "tops",
    "t-shirt": "tops",
    "tee": "tops",
    "tees": "tops",
    "sleeveless": "tops",
    "tank": "tops",
    "tank-top": "tops",
    "blouse": "tops",
    "hoodie": "tops",
    "hoody": "tops",
    "sweatshirt": "tops",
    "polo": "tops",
    "henley": "tops",
    "crop-top": "tops",
    "croptop": "tops",
    # knitwear
    "sweater": "knitwear",
    "sweaters": "knitwear",
    "knit": "knitwear",
    "cardigan": "knitwear",
    "pullover": "knitwear",
    "jumper": "knitwear",
    # bottoms
    "skirt": "bottoms",
    "skirts": "bottoms",
    "pants": "bottoms",
    "pant": "bottoms",
    "jeans": "bottoms",
    "denim": "bottoms",
    "shorts": "bottoms",
    "trousers": "bottoms",
    "leggings": "bottoms",
    "slacks": "bottoms",
    "skort": "bottoms",
    # outerwear
    "jacket": "outerwear",
    "blazer": "outerwear",
    "coat": "outerwear",
    "parka": "outerwear",
    "trench": "outerwear",
    "trenchcoat": "outerwear",
    "puffer": "outerwear",
    "vest": "outerwear",
    "windbreaker": "outerwear",
    # shoes (2026-07-15: sneakers family 는 상품 매핑에 미사용 — shoes 로 통합)
    "sneaker": "shoes",
    "sneakers": "shoes",
    "trainers": "shoes",
    "boots": "shoes",
    "boot": "shoes",
    "loafers": "shoes",
    "loafer": "shoes",
    "heels": "shoes",
    "heel": "shoes",
    "sandals": "shoes",
    "sandal": "shoes",
    "mules": "shoes",
    "flats": "shoes",
    # bags
    "bags": "bags",
    "handbag": "bags",
    "tote": "bags",
    "backpack": "bags",
    "clutch": "bags",
    "crossbody": "bags",
    # headwear
    "jewellery": "jewelry",
    "glasses": "eyewear",
    "sunglasses": "eyewear",
    "hat": "headwear",
    "hats": "headwear",
    "cap": "headwear",
    "caps": "headwear",
    "beanie": "headwear",
    "beret": "headwear",
    "bucket-hat": "headwear",
    "buckethat": "headwear",
    # dresses (alias for plural / variants)
    "dresses": "dresses",
    "mini-dress": "dresses",
    "minidress": "dresses",
    "maxi-dress": "dresses",
    "maxidress": "dresses",
    "midi-dress": "dresses",
    "mididress": "dresses",
    "jumpsuit": "dresses",
    # swimwear / activewear
    "swimsuit": "swimwear",
    "bikini": "swimwear",
    "trunks": "swimwear",
    "tracksuit": "activewear",
    "sportswear": "activewear",
    "athletic": "activewear",
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
    The resolved token then passes `_FAMILY_REMAP` (sneakers→shoes — the
    sneakers family has zero product rows since the 2026-07 backend
    normalization, so gating on it would silently kill the family gate).

    Always returns exactly ONE lowercase token from `CANONICAL_FAMILIES`;
    NEVER returns None (the v6 contract requires `p_category` to be exactly
    one lowercase token).
    """
    t = (raw or "").strip().lower()
    if t in CANONICAL_FAMILIES:
        return _FAMILY_REMAP.get(t, t)
    if t in _VISION_ALIAS:
        t = _VISION_ALIAS[t]
        return _FAMILY_REMAP.get(t, t)
    return "other"


__all__ = [
    "CANONICAL_FAMILIES",
    "to_canonical_family",
]
