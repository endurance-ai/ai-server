"""Critique parser tests — callback-based (deterministic) + free-text fallback."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.channels import critique


@dataclass
class _C:
    id: str
    brand: str = ""
    name: str = ""
    price: int | None = None


def test_callback_more_returns_anchor_only():
    last = [_C("p1", "ami", "tee", 50000), _C("p2", "mfpen", "shirt", 80000)]
    d = critique.parse_callback("crit:more:1", last)
    assert d is not None
    assert d.op == "more"
    assert d.anchor and d.anchor.idx == 1
    assert d.anchor.brand == "mfpen"
    assert d.exclude_brands == []


def test_callback_less_excludes_anchor_brand():
    last = [_C("p1", "Zara", "denim jacket", 90000)]
    d = critique.parse_callback("crit:less:0", last)
    assert d is not None
    assert d.op == "less"
    assert d.exclude_brands == ["Zara"]


def test_callback_cheap_sets_max_price_at_default_ratio():
    last = [_C("p1", "ami", "tee", 100000)]
    d = critique.parse_callback("crit:cheap:0", last)
    assert d is not None
    assert d.op == "cheap"
    # default CRITIQUE_CHEAPER_RATIO = 0.7 → 70_000
    assert d.max_price == 70000


def test_callback_invalid_format_returns_none():
    assert critique.parse_callback("garbage", [_C("p1")]) is None
    assert critique.parse_callback("crit:more:abc", [_C("p1")]) is None


def test_callback_out_of_range_returns_none():
    assert critique.parse_callback("crit:more:5", [_C("p1")]) is None


def test_callback_no_price_yields_no_max_price():
    last = [_C("p1", "ami", "tee", None)]
    d = critique.parse_callback("crit:cheap:0", last)
    assert d is not None
    assert d.max_price is None


@pytest.mark.asyncio
async def test_parse_text_falls_back_when_router_disabled(monkeypatch):
    monkeypatch.setattr(critique.settings, "ROUTER_LLM_ENABLED", False)
    d = await critique.parse_text("less denim, in beige", [])
    assert d.op == "free_text"
    assert d.extra_intent and "less denim" in d.extra_intent


@pytest.mark.asyncio
async def test_parse_text_empty_input_is_noop():
    d = await critique.parse_text("   ", [])
    assert d.op == "noop"
