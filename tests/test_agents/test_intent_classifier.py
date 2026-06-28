"""Unit tests for the modifier-intent classifier.

Maps a refine follow-up message to one of five intent kinds via an injectable
`litellm_call`. All paths fall back to `free_form` on failure. Covers the pure
`_parse_json_relaxed` / `_normalise_result` helpers and the async
`classify_intent` flow with a stub LLM.
"""

from __future__ import annotations

from app.agents import intent_classifier as ic
from app.agents.intent_classifier import IntentResult, classify_intent


def _stub(content: str):
    async def _call(*, model, messages):
        return content

    return _call


# ── _parse_json_relaxed ─────────────────────────────────────────────────────


def test_parse_plain_json():
    assert ic._parse_json_relaxed('{"intent": "free_form"}') == {"intent": "free_form"}


def test_parse_strips_code_fence():
    assert ic._parse_json_relaxed('```json\n{"intent": "mood_shift"}\n```') == {"intent": "mood_shift"}


def test_parse_extracts_embedded_object():
    assert ic._parse_json_relaxed('here you go: {"intent": "color_swap"} ok') == {"intent": "color_swap"}


def test_parse_unrecoverable_returns_none():
    assert ic._parse_json_relaxed("not json at all") is None


def test_parse_malformed_embedded_returns_none():
    assert ic._parse_json_relaxed("{ broken : }") is None


# ── _normalise_result ───────────────────────────────────────────────────────


def test_normalise_invalid_intent_falls_back():
    res = ic._normalise_result({"intent": "teleport"})
    assert res.intent == "free_form"
    assert res.confidence == 0.0


def test_normalise_color_swap_keeps_attrs():
    res = ic._normalise_result(
        {"intent": "color_swap", "from_attribute": "Grey", "to_attribute": "NAVY", "confidence": 0.9}
    )
    assert res.intent == "color_swap"
    assert res.from_attribute == "grey"
    assert res.to_attribute == "navy"
    assert res.confidence == 0.9


def test_normalise_mood_shift_drops_attrs():
    res = ic._normalise_result(
        {"intent": "mood_shift", "from_attribute": "grey", "to_attribute": "navy", "confidence": 0.5}
    )
    # attrs only retained for color_swap / fit_change.
    assert res.from_attribute is None
    assert res.to_attribute is None


def test_normalise_clamps_confidence():
    assert ic._normalise_result({"intent": "free_form", "confidence": 5.0}).confidence == 1.0
    assert ic._normalise_result({"intent": "free_form", "confidence": -2.0}).confidence == 0.0


def test_normalise_non_numeric_confidence_is_zero():
    assert ic._normalise_result({"intent": "free_form", "confidence": "high"}).confidence == 0.0


def test_normalise_empty_attr_becomes_none():
    res = ic._normalise_result({"intent": "fit_change", "from_attribute": "", "to_attribute": "  "})
    assert res.from_attribute is None
    assert res.to_attribute is None


# ── classify_intent flow ────────────────────────────────────────────────────


async def test_empty_message_falls_back_without_calling_llm():
    called = False

    async def _call(*, model, messages):
        nonlocal called
        called = True
        return "{}"

    res = await classify_intent("   ", litellm_call=_call)
    assert res.intent == "free_form"
    assert called is False


async def test_valid_color_swap_response():
    content = '{"intent": "color_swap", "from_attribute": "grey", "to_attribute": "blue", "confidence": 0.8}'
    res = await classify_intent("make it blue", "grey overcoat", litellm_call=_stub(content))
    assert isinstance(res, IntentResult)
    assert res.intent == "color_swap"
    assert res.to_attribute == "blue"


async def test_llm_raise_falls_back():
    async def _boom(*, model, messages):
        raise RuntimeError("litellm down")

    res = await classify_intent("더 캐주얼하게", litellm_call=_boom)
    assert res.intent == "free_form"


async def test_unparseable_response_falls_back():
    res = await classify_intent("blah", litellm_call=_stub("totally not json"))
    assert res.intent == "free_form"


async def test_empty_llm_content_falls_back():
    res = await classify_intent("blah", litellm_call=_stub(""))
    assert res.intent == "free_form"
