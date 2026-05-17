"""Router tests — fallback paths (LLM disabled / failing) + JSON parsing.

Real LLM calls are not exercised; we verify the deterministic envelope so
the bot stays usable without a configured LiteLLM proxy.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.channels import router
from app.infrastructure.memory.session import SessionState


@dataclass
class _C:
    id: str = "p1"
    brand: str = "ami"
    name: str = "tee"
    price: int = 50000


@pytest.mark.asyncio
async def test_router_falls_back_to_critique_in_results_sent_when_llm_disabled(monkeypatch):
    monkeypatch.setattr(router.settings, "ROUTER_LLM_ENABLED", False)
    decision = await router.route_text("less denim please", SessionState.RESULTS_SENT, [_C()])
    assert decision.intent == router.RoutedIntent.CRITIQUE_TEXT
    assert decision.critique_delta is not None
    assert "less denim" in (decision.critique_delta.extra_intent or "")


@pytest.mark.asyncio
async def test_router_falls_back_to_off_topic_in_idle_when_llm_disabled(monkeypatch):
    monkeypatch.setattr(router.settings, "ROUTER_LLM_ENABLED", False)
    decision = await router.route_text("hello there", SessionState.IDLE, [])
    assert decision.intent == router.RoutedIntent.OFF_TOPIC


@pytest.mark.asyncio
async def test_router_empty_text_is_off_topic():
    decision = await router.route_text("   ", SessionState.RESULTS_SENT, [])
    assert decision.intent == router.RoutedIntent.OFF_TOPIC


def test_parse_router_output_extracts_critique_fields():
    raw = (
        '{"intent": "critique_text", "critique": {'
        '"exclude_brands": ["Zara"], "exclude_keywords": ["denim"], '
        '"boost_keywords": ["wool"], "max_price": 80000, "min_price": null, '
        '"color": "beige", "extra_intent": "more elegant"}, "taste": null}'
    )
    decision = router._parse_router_output(raw, "less denim, in beige, under 80k")
    assert decision.intent == router.RoutedIntent.CRITIQUE_TEXT
    d = decision.critique_delta
    assert d is not None
    assert d.exclude_brands == ["Zara"]
    assert d.exclude_keywords == ["denim"]
    assert d.boost_keywords == ["wool"]
    assert d.max_price == 80000
    assert d.min_price is None
    assert d.color == "beige"
    assert d.extra_intent == "more elegant"


def test_parse_router_output_extracts_taste_update():
    raw = (
        '{"intent": "taste_update", "critique": null, "taste": {'
        '"liked_brands": ["ami", "mfpen"], "disliked_brands": ["fast fashion"], '
        '"liked_keywords": ["minimal"], "disliked_keywords": []}}'
    )
    decision = router._parse_router_output(raw, "I like ami and mfpen, hate fast fashion")
    assert decision.intent == router.RoutedIntent.TASTE_UPDATE
    t = decision.taste_update
    assert t is not None
    assert t.liked_brands == ["ami", "mfpen"]
    assert t.disliked_brands == ["fast fashion"]
    assert t.liked_keywords == ["minimal"]


def test_parse_router_output_falls_back_on_garbage():
    decision = router._parse_router_output("not json at all", "less denim")
    # Falls back to critique_text with raw text as extra_intent
    assert decision.intent == router.RoutedIntent.CRITIQUE_TEXT
    assert decision.critique_delta is not None
    assert decision.critique_delta.extra_intent == "less denim"
