"""Unit tests for critique parsing (callback + free-text).

`parse_callback` is deterministic (no LLM). `parse_text` is LLM-backed but
always returns a CritiqueDelta — tests exercise the empty/noop guard, the
flag-off fallback, the mocked-LLM happy path, and LLM-failure fallback.
"""

from __future__ import annotations

import json

import httpx

from app.channels import critique
from app.channels.critique import CritiqueDelta, parse_callback, parse_text
from app.core.config import settings


def _card(idx, **kw):
    base = {"id": f"p{idx}", "brand": "Acme", "name": "Coat", "price": 100_000}
    base.update(kw)
    return base


_RESULTS = [_card(0), _card(1, brand="Zenith", price=50_000)]


# ── parse_callback ──────────────────────────────────────────────────────────


def test_callback_invalid_format_returns_none():
    assert parse_callback("garbage", _RESULTS) is None
    assert parse_callback("", _RESULTS) is None
    assert parse_callback("crit:bogus:0", _RESULTS) is None


def test_callback_out_of_range_index_returns_none():
    assert parse_callback("crit:more:9", _RESULTS) is None


def test_callback_more_records_anchor():
    delta = parse_callback("crit:more:0", _RESULTS)
    assert delta.op == "more"
    assert delta.anchor.idx == 0
    assert delta.anchor.product_id == "p0"


def test_callback_less_excludes_brand():
    delta = parse_callback("crit:less:1", _RESULTS)
    assert delta.op == "less"
    assert delta.exclude_brands == ["Zenith"]


def test_callback_cheap_computes_max_price():
    delta = parse_callback("crit:cheap:0", _RESULTS)
    assert delta.op == "cheap"
    # 100_000 * CRITIQUE_CHEAPER_RATIO (0.7) = 70_000
    assert delta.max_price == int(100_000 * settings.CRITIQUE_CHEAPER_RATIO)


def test_callback_cheap_zero_price_no_max():
    delta = parse_callback("crit:cheap:0", [_card(0, price=0)])
    assert delta.op == "cheap"
    assert delta.max_price is None


def test_callback_works_with_object_candidates():
    from types import SimpleNamespace

    cards = [SimpleNamespace(id="x1", brand="Beta", name="Bag", price=20_000)]
    delta = parse_callback("crit:less:0", cards)
    assert delta.exclude_brands == ["Beta"]


# ── parse_text ──────────────────────────────────────────────────────────────


async def test_text_empty_returns_noop():
    delta = await parse_text("   ", _RESULTS)
    assert delta.op == "noop"


async def test_text_flag_off_returns_free_text_fallback(monkeypatch):
    monkeypatch.setattr(settings, "ROUTER_LLM_ENABLED", False)
    delta = await parse_text("no denim please", _RESULTS)
    assert delta.op == "free_text"
    assert delta.extra_intent == "no denim please"


async def test_text_llm_success_parses_delta(monkeypatch):
    monkeypatch.setattr(settings, "ROUTER_LLM_ENABLED", True)

    payload = {
        "exclude_brands": ["acme"],
        "exclude_keywords": ["denim"],
        "boost_keywords": ["linen"],
        "max_price": 200000,
        "min_price": None,
        "color": "navy",
        "extra_intent": "summer vibe",
    }

    async def _fake_chat(**kwargs):
        return {"choices": [{"message": {"content": json.dumps(payload)}}]}

    monkeypatch.setattr(critique.LLMProvider, "chat", _fake_chat)
    delta = await parse_text("less denim, more linen under 200k navy", _RESULTS)
    assert delta.op == "free_text"
    assert delta.exclude_brands == ["acme"]
    assert delta.exclude_keywords == ["denim"]
    assert delta.boost_keywords == ["linen"]
    assert delta.max_price == 200000
    assert delta.min_price is None
    assert delta.color == "navy"
    assert delta.extra_intent == "summer vibe"


async def test_text_llm_timeout_returns_fallback(monkeypatch):
    monkeypatch.setattr(settings, "ROUTER_LLM_ENABLED", True)

    async def _boom(**kwargs):
        raise httpx.HTTPError("connection reset")

    monkeypatch.setattr(critique.LLMProvider, "chat", _boom)
    delta = await parse_text("anything", _RESULTS)
    assert delta.op == "free_text"
    assert delta.extra_intent == "anything"


async def test_text_llm_bad_content_returns_fallback(monkeypatch):
    monkeypatch.setattr(settings, "ROUTER_LLM_ENABLED", True)

    async def _fake_chat(**kwargs):
        return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setattr(critique.LLMProvider, "chat", _fake_chat)
    delta = await parse_text("anything", _RESULTS)
    assert isinstance(delta, CritiqueDelta)
    assert delta.op == "free_text"
    assert delta.extra_intent == "anything"


async def test_text_embedded_json_recovered(monkeypatch):
    monkeypatch.setattr(settings, "ROUTER_LLM_ENABLED", True)

    async def _fake_chat(**kwargs):
        return {"choices": [{"message": {"content": 'sure: {"boost_keywords": ["wool"]} done'}}]}

    monkeypatch.setattr(critique.LLMProvider, "chat", _fake_chat)
    delta = await parse_text("more wool", _RESULTS)
    assert delta.boost_keywords == ["wool"]
