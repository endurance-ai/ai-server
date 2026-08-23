"""SPEC-AGENT-V2-REACT / REQ-AGENT-TOOL-CATALOG-001 + REQ-AGENT-TOOL-DISPATCH-001.

Registry shape + args validation. Each tool wrapper module is asserted import-able.
"""

from __future__ import annotations

import importlib

import pytest

from app.agents.tool_registry import REGISTRY, TOOL_NAMES, validate_args


def test_registry_has_9_tools():
    # SPEC-AGENT-V2-CLEANUP-001 — suggest_next_step is now unconditional.
    # 2026-08 — web_search added (gated on TAVILY_API_KEY at LLM-exposure time,
    # but always present in REGISTRY for dispatch).
    assert len(REGISTRY) == 9
    expected = {
        "analyze_image",
        "search_products",
        "refine_search",
        "update_taste",
        "ask_user_clarification",
        "get_recent_history",
        "web_search",
        "respond",
        "suggest_next_step",
    }
    assert set(TOOL_NAMES) == expected


def test_each_tool_has_typeddict_args_result():
    for name, meta in REGISTRY.items():
        assert "args_typeddict" in meta and meta["args_typeddict"] is not None, name
        assert "result_typeddict" in meta and meta["result_typeddict"] is not None, name
        assert "description" in meta and meta["description"], name
        assert "langfuse_span_tag" in meta and meta["langfuse_span_tag"], name
        assert "terminates_loop" in meta, name


def test_only_respond_terminates_loop():
    for name, meta in REGISTRY.items():
        if name == "respond":
            assert meta["terminates_loop"] is True
        else:
            assert meta["terminates_loop"] is False


@pytest.mark.parametrize("name", list(REGISTRY.keys()))
def test_dispatcher_resolves(name):
    meta = REGISTRY[name]
    module_path, fn_name = meta["dispatch_fn_path"].split(":")
    mod = importlib.import_module(module_path)
    fn = getattr(mod, fn_name)
    assert callable(fn), f"{name} dispatcher not callable"


def test_validate_args_unknown_tool():
    ok, err = validate_args("nonexistent_tool", {})
    assert ok is False
    assert "unknown_tool" in err


def test_validate_args_not_dict():
    ok, err = validate_args("respond", "not-a-dict")  # type: ignore[arg-type]
    assert ok is False
    assert err == "args_not_dict"


def test_validate_args_unknown_keys():
    ok, err = validate_args("respond", {"text": "hi", "evil_field": True})
    assert ok is False
    assert "unknown_keys" in err


def test_validate_args_accepts_minimal():
    ok, err = validate_args("respond", {"text": "hi"})
    assert ok is True, err


# ── P1-C: int-only top_k/n vs int|float min_price/max_price ───────────────


def test_validate_args_accepts_float_price():
    """LLM legitimately sends e.g. 59.99 — must not be rejected (P1-C)."""
    ok, err = validate_args("search_products", {"text_query": "loafers", "min_price": 59.99, "max_price": 120.0})
    assert ok is True, err
    ok, err = validate_args("refine_search", {"action": "cheaper", "max_price": 49.95})
    assert ok is True, err


def test_validate_args_n_is_int_only():
    # NOTE (2026-05-20): search_products.top_k removed from LLM schema —
    # int-only validation now tested via get_recent_history.n (the surviving
    # int field). Coercion logic identical for both.
    ok, err = validate_args("get_recent_history", {"n": 12})
    assert ok is True, err
    ok, err = validate_args("get_recent_history", {"n": 12.5})
    assert ok is False
    assert "n must be int" in err


@pytest.mark.parametrize(
    ("tool", "args", "field"),
    [
        ("get_recent_history", {"n": True}, "n must be int"),
        ("search_products", {"text_query": "x", "min_price": True}, "min_price must be number"),
        ("search_products", {"text_query": "x", "max_price": False}, "max_price must be number"),
    ],
)
def test_validate_args_rejects_bool(tool, args, field):
    """bool is an int subclass — rejected for both int-only and number groups."""
    ok, err = validate_args(tool, args)
    assert ok is False
    assert field in err


# ── P1-2/4: analyze_image no longer accepts an LLM-supplied image_url ──────


def test_analyze_image_takes_no_args():
    ok, err = validate_args("analyze_image", {})
    assert ok is True, err


def test_analyze_image_image_url_rejected_as_unknown_key():
    """SSRF surface removed: any supplied image_url → unknown_keys (P1-2/4)."""
    ok, err = validate_args("analyze_image", {"image_url": "http://2130706433/"})
    assert ok is False
    assert "unknown_keys" in err
