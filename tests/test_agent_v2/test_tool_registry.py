"""SPEC-AGENT-V2-REACT / REQ-AGENT-TOOL-CATALOG-001 + REQ-AGENT-TOOL-DISPATCH-001.

Registry shape + args validation. Each tool wrapper module is asserted import-able.
"""

from __future__ import annotations

import importlib

import pytest

from app.agents.tool_registry import REGISTRY, TOOL_NAMES, validate_args


def test_registry_has_7_tools():
    assert len(REGISTRY) == 7
    expected = {
        "analyze_image",
        "search_products",
        "refine_search",
        "update_taste",
        "ask_user_clarification",
        "get_recent_history",
        "respond",
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
