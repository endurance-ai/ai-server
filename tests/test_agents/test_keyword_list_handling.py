"""boost_keywords / exclude_keywords — string vs list handling (2026-05-20).

Three independent fixes cover one production defect (LLM passing a string
instead of a list → `list("t-shirt")` → ["t","-","s","h","i","r","t"] in the
embedded query).

A. refine_search dispatch defensive cast (str → [str], list/tuple → list[str])
B. validate_args strict list-type rejection for boost_keywords / exclude_keywords
C. search_products writes LLM-supplied (English) text_query into ctx so a
   subsequent refine_search reuses it instead of the raw Korean user message
"""

from __future__ import annotations

from app.agents.tool_registry import validate_args
from app.agents.tools.refine_search import _as_keyword_list_for_test  # type: ignore[attr-defined]

# ─── A: defensive cast — _as_keyword_list ─────────────────────────────────


def test_defensive_cast_string_becomes_single_element_list():
    assert _as_keyword_list_for_test("t-shirt") == ["t-shirt"]


def test_defensive_cast_empty_string_returns_empty():
    assert _as_keyword_list_for_test("") == []
    assert _as_keyword_list_for_test("   ") == []


def test_defensive_cast_none_returns_empty():
    assert _as_keyword_list_for_test(None) == []


def test_defensive_cast_list_passes_through():
    assert _as_keyword_list_for_test(["t-shirt", "denim"]) == ["t-shirt", "denim"]


def test_defensive_cast_tuple_becomes_list():
    assert _as_keyword_list_for_test(("a", "b")) == ["a", "b"]


def test_defensive_cast_drops_falsy_in_list():
    assert _as_keyword_list_for_test(["a", "", None, "b"]) == ["a", "b"]


def test_defensive_cast_unknown_type_returns_empty():
    assert _as_keyword_list_for_test(42) == []
    assert _as_keyword_list_for_test({"k": "v"}) == []


# ─── B: validate_args strict rejection ────────────────────────────────────


def test_validate_rejects_string_boost_keywords_for_refine_search():
    ok, err = validate_args("refine_search", {"boost_keywords": "t-shirt"})
    assert ok is False
    assert "boost_keywords" in (err or "")
    assert "must be list" in (err or "")


def test_validate_rejects_string_exclude_keywords_for_refine_search():
    ok, err = validate_args("refine_search", {"exclude_keywords": "jacket"})
    assert ok is False
    assert "exclude_keywords" in (err or "")


def test_validate_accepts_list_boost_keywords_for_refine_search():
    ok, err = validate_args("refine_search", {"boost_keywords": ["t-shirt"]})
    assert ok is True
    assert err is None


def test_validate_accepts_empty_list_boost_keywords():
    ok, err = validate_args("refine_search", {"boost_keywords": []})
    assert ok is True
    assert err is None


def test_validate_passes_when_keyword_fields_absent():
    ok, err = validate_args("refine_search", {})
    assert ok is True
    assert err is None
