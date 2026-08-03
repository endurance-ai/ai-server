"""SPEC-SEARCH-V6-001 — `to_canonical_family` unit suite.

Pure-function contract for the v6 canonical-family gate normalizer:
  - all 20 canonical tokens → identity passthrough;
  - the 7 bot Vision-enum aliases → correct canonical family;
  - case / whitespace insensitive;
  - any unknown / non-apparel / style-node-letter / Korean → "other";
  - None / "" → "other";
  - every alias target is one of the 20 canonical tokens;
  - NEVER returns None.
"""

from __future__ import annotations

import pytest

from app.infrastructure.repositories.category_family import (
    _VISION_ALIAS,
    CANONICAL_FAMILIES,
    to_canonical_family,
)

# The 20 canonical families (lowercase, verbatim) — duplicated here on purpose
# so a drift in the module set is caught (this is the locked spec list).
_EXPECTED_20 = {
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


def test_canonical_set_is_exactly_the_20_spec_tokens():
    assert CANONICAL_FAMILIES == _EXPECTED_20
    assert len(CANONICAL_FAMILIES) == 20


@pytest.mark.parametrize("token", sorted(_EXPECTED_20))
def test_all_20_canonical_tokens_identity_passthrough(token):
    # 2026-07-15 — sneakers 는 identity 예외: products.category 정규화(14+other)
    # 이후 sneakers family 상품이 0행이라 shoes 로 리맵된다 (게이트 무력화 방지).
    expected = "shoes" if token == "sneakers" else token
    assert to_canonical_family(token) == expected


# The 7 bot Vision enum values (vision_prompt.py:150) → canonical family.
@pytest.mark.parametrize(
    ("vision_raw", "expected"),
    [
        ("Outer", "outerwear"),
        ("Top", "tops"),
        ("Bottom", "bottoms"),
        ("Shoes", "shoes"),
        ("Bag", "bags"),
        ("Dress", "dresses"),
        ("Accessories", "accessories"),
    ],
)
def test_vision_seven_enum_maps_to_canonical(vision_raw, expected):
    assert to_canonical_family(vision_raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  TOP ", "tops"),
        ("\toUtEr\n", "outerwear"),
        ("  shoes  ", "shoes"),
        ("DRESS", "dresses"),
    ],
)
def test_case_and_whitespace_insensitive(raw, expected):
    assert to_canonical_family(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("jewellery", "jewelry"),
        ("glasses", "eyewear"),
        ("sunglasses", "eyewear"),
        ("hat", "headwear"),
        ("cap", "headwear"),
    ],
)
def test_conservative_defensive_synonyms(raw, expected):
    assert to_canonical_family(raw) == expected
    assert expected in CANONICAL_FAMILIES


@pytest.mark.parametrize(
    "raw",
    [
        "Earrings",  # Vision subcategory, NOT a family
        "악세사리",  # Korean — not in the alias map
        "blah",
        "C",  # brand style-node letter (A–Z) — the old mislabel bug input
        "apparel",  # legacy AnalyzedItem placeholder
        "footwear",  # plausible synonym we deliberately did NOT add
        "menswear",
    ],
)
def test_unknown_strings_fall_to_other(raw):
    assert to_canonical_family(raw) == "other"


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_none_and_blank_resolve_to_other(raw):
    assert to_canonical_family(raw) == "other"


def test_every_alias_target_is_a_canonical_token():
    for src, target in _VISION_ALIAS.items():
        assert target in CANONICAL_FAMILIES, f"{src}→{target} target not in 20 canonical"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 260611 — LLM-emitted garment vocabulary expansion. Each entry below
        # used to fall through to "other" → family gate skipped → cosine-only
        # ranking. The expanded `_VISION_ALIAS` engages the gate for each.
        ("shirt", "tops"),
        ("hoodie", "tops"),
        ("sleeveless", "tops"),
        ("tee", "tops"),
        ("sweater", "knitwear"),
        ("knit", "knitwear"),
        ("cardigan", "knitwear"),
        ("skirt", "bottoms"),
        ("jeans", "bottoms"),
        ("pants", "bottoms"),
        ("shorts", "bottoms"),
        ("jacket", "outerwear"),
        ("blazer", "outerwear"),
        ("coat", "outerwear"),
        ("trench", "outerwear"),
        ("sneaker", "shoes"),
        ("boots", "shoes"),
        ("loafers", "shoes"),
        ("heels", "shoes"),
        ("beanie", "headwear"),
        ("bucket-hat", "headwear"),
        ("backpack", "bags"),
        ("tote", "bags"),
        ("jumpsuit", "dresses"),
        ("swimsuit", "swimwear"),
    ],
)
def test_expanded_llm_vocabulary_engages_family_gate(raw, expected):
    assert to_canonical_family(raw) == expected


def test_never_returns_none():
    for raw in (None, "", "garbage", "Top", "shoes", "C", 0, "  "):
        assert to_canonical_family(raw if isinstance(raw, (str, type(None))) else str(raw)) is not None


# 2026-07-15 — products.category 정규화(14 family + other) 반영: sneakers
# family 는 상품 행이 0 이라 게이트를 걸면 rung 3 폴백으로 family gate 가
# 통째로 무력화된다. sneakers 로 resolve 되는 모든 입력은 shoes 로 리맵.
@pytest.mark.parametrize("raw", ["sneakers", "Sneakers", "sneaker", "trainers", " SNEAKERS "])
def test_sneakers_family_remapped_to_shoes(raw):
    assert to_canonical_family(raw) == "shoes"
