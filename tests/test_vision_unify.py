"""SPEC-VISION-UNIFY-001 — RED tests for the unified Vision schema.

Covers:
- REQ-VISION-UNIFY-001: prompt module exposes ANALYZE_SYSTEM_PROMPT with
  expected marker substrings.
- REQ-VISION-UNIFY-002: extract() returns VisionResult; fallback on failure
  satisfies the schema with documented placeholder values.
- REQ-VISION-UNIFY-003: max_tokens=2500, temperature=0.3 reach LLMProvider.chat.
- REQ-VISION-WEAKVISION-001: 5 rules covered.
- REQ-VISION-COMPAT-005: VISION_SCHEMA_V2=False reverts to legacy behavior.
- REQ-VISION-SEARCH-002: rich fields map into RecommendRequest.
- REQ-VISION-SEARCH-001: ChannelRecommendationRequest accepts new fields.
"""

from __future__ import annotations

import pytest

from app.channels import vision as vision_module
from app.channels.recommendation import (
    ChannelRecommendationRequest,
    PipelineRecommendationPort,
)
from app.channels.vision import (
    VisionItem,
    VisionMood,
    VisionMoodTag,
    VisionPaletteEntry,
    VisionPosition,
    VisionResult,
    VisionStyle,
    VisionStyleNode,
    derive_legacy_keywords,
    derive_legacy_label,
    extract,
)
from app.channels.vision_prompt import (
    ANALYZE_SYSTEM_PROMPT,
    ANALYZE_USER_PROMPT,
    SENSITIVITY_TAGS,
    STYLE_NODE_IDS,
)
from app.core.config import settings

# ── REQ-VISION-UNIFY-001 — prompt module markers ───────────────────────────


class TestPromptModule:
    def test_prompt_contains_required_markers(self):
        for marker in ("isApparel", "styleNode", "sensitivityTags", "searchQueryKo", "colorFamily"):
            assert marker in ANALYZE_SYSTEM_PROMPT, f"missing marker: {marker}"

    def test_prompt_no_unfilled_template_literals(self):
        # Smoke check against R10: ${...} placeholders must not survive the
        # JS → Python port.
        assert "${" not in ANALYZE_SYSTEM_PROMPT, "JS template literal slot leaked into prompt"

    def test_prompt_includes_node_taxonomy(self):
        # Spot-check: node IDs and sensitivity tags appear inline.
        assert "[A-1]" in ANALYZE_SYSTEM_PROMPT
        assert "[C]" in ANALYZE_SYSTEM_PROMPT
        assert "미니멀" in ANALYZE_SYSTEM_PROMPT

    def test_user_prompt_matches_portal_app(self):
        assert ANALYZE_USER_PROMPT.startswith("Analyze this outfit photo")

    def test_node_ids_match_portal_app(self):
        assert "C" in STYLE_NODE_IDS
        assert "A-1" in STYLE_NODE_IDS
        assert len(STYLE_NODE_IDS) == 15

    def test_sensitivity_tags_match_portal_app(self):
        assert "미니멀" in SENSITIVITY_TAGS
        assert "고프코어" in SENSITIVITY_TAGS
        assert len(SENSITIVITY_TAGS) == 12


# ── REQ-VISION-UNIFY-002 — VisionResult Pydantic model + safe fallback ────


class TestVisionResultModel:
    def test_default_construction(self):
        r = VisionResult()
        assert r.isApparel is False
        assert r.items == []
        assert r.styleNode.primary == "C"
        assert r.style.detectedGender == "unisex"

    def test_full_construction(self):
        r = VisionResult(
            isApparel=True,
            styleNode=VisionStyleNode(primary="C", primaryConfidence=0.85),
            sensitivityTags=["미니멀"],
            mood=VisionMood(tags=[VisionMoodTag(label="Street", score=92.0)], summary="x"),
            palette=[VisionPaletteEntry(hex="#000000", label="Black")],
            style=VisionStyle(fit="oversized", aesthetic="minimal", detectedGender="male"),
            items=[
                VisionItem(
                    id="top",
                    category="Top",
                    subcategory="t-shirt",
                    name="Boxy Tee",
                    fabric="jersey",
                    color="black",
                    colorHex="#000000",
                    colorFamily="BLACK",
                    fit="boxy",
                    searchQuery="boxy black jersey t-shirt men",
                    searchQueryKo="박시 블랙 저지 티셔츠 남성",
                    position=VisionPosition(top=42.0, left=48.0),
                ),
            ],
        )
        assert r.items[0].searchQueryKo == "박시 블랙 저지 티셔츠 남성"
        assert r.items[0].colorFamily == "BLACK"

    def test_fallback_satisfies_schema(self):
        r = vision_module._fallback_result()
        assert isinstance(r, VisionResult)
        assert r.isApparel is False
        assert r.items == []
        assert r.styleNode.primary == "C"
        assert r.style.detectedGender == "unisex"


class TestExtractFallback:
    @pytest.mark.asyncio
    async def test_timeout_returns_fallback(self, monkeypatch):
        async def _slow(**kw):
            raise TimeoutError("boom")

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _slow)
        # Wrap in asyncio.wait_for so TimeoutError is what the function sees.
        # extract() itself uses asyncio.wait_for; an immediate raise of
        # TimeoutError simulates the case post-cancellation.
        result = await extract("https://example.com/x.jpg")
        assert isinstance(result, VisionResult)
        assert result.isApparel is False
        assert result.items == []

    @pytest.mark.asyncio
    async def test_http_error_returns_fallback(self, monkeypatch):
        async def _err(**kw):
            raise RuntimeError("HTTP 500")

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _err)
        result = await extract("https://example.com/x.jpg")
        assert isinstance(result, VisionResult)
        assert result.isApparel is False

    @pytest.mark.asyncio
    async def test_malformed_json_returns_fallback(self, monkeypatch):
        async def _bad(**kw):
            return {"choices": [{"message": {"content": "not json at all"}}]}

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _bad)
        result = await extract("https://example.com/x.jpg")
        assert isinstance(result, VisionResult)
        assert result.isApparel is False


# ── REQ-VISION-UNIFY-003 — parity max_tokens / temperature / timeout ──────


class TestExtractParameters:
    @pytest.mark.asyncio
    async def test_v2_uses_2500_max_tokens_and_temp_0_3(self, monkeypatch):
        captured = {}

        async def _capture(*, model, messages, temperature, max_tokens):
            captured["model"] = model
            captured["temperature"] = temperature
            captured["max_tokens"] = max_tokens
            captured["system"] = messages[0]["content"]
            return {"choices": [{"message": {"content": '{"isApparel": false, "items": []}'}}]}

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _capture)
        monkeypatch.setattr(settings, "VISION_SCHEMA_V2", True)
        monkeypatch.setattr(settings, "VISION_MAX_TOKENS", 2500)
        monkeypatch.setattr(settings, "VISION_TEMPERATURE", 0.3)

        await extract("https://example.com/x.jpg")
        assert captured["max_tokens"] == 2500
        assert captured["temperature"] == 0.3
        # Verify the rich system prompt is used.
        assert "isApparel" in captured["system"]


# ── REQ-VISION-COMPAT-005 — flag rollback to legacy schema ─────────────────


class TestSchemaV2Flag:
    @pytest.mark.asyncio
    async def test_v2_off_uses_legacy_prompt_and_params(self, monkeypatch):
        captured = {}

        async def _capture(*, model, messages, temperature, max_tokens):
            captured["temperature"] = temperature
            captured["max_tokens"] = max_tokens
            captured["system"] = messages[0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"items": [{"label":"white tee",'
                                '"description":"crew slim","color":"white","keywords":["tee","white"]}]}'
                            )
                        }
                    }
                ]
            }

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _capture)
        monkeypatch.setattr(settings, "VISION_SCHEMA_V2", False)

        result = await extract("https://example.com/x.jpg")
        assert isinstance(result, VisionResult)
        # Legacy path: temperature=0.2, max_tokens=600 (per the legacy behavior).
        assert captured["temperature"] == 0.2
        assert captured["max_tokens"] == 600
        # Legacy system prompt does NOT contain `isApparel`.
        assert "isApparel" not in captured["system"]
        # Legacy result still adapts to VisionResult — searchQueryKo empty.
        assert result.items
        assert result.items[0].searchQueryKo == ""


# ── REQ-VISION-WEAKVISION-001 — 5 rules ────────────────────────────────────


def _make_strong_item() -> VisionItem:
    return VisionItem(
        id="top_1",
        category="Top",
        subcategory="t-shirt",
        name="Boxy Tee",
        fabric="jersey",
        color="black",
        colorFamily="BLACK",
        fit="boxy",
        searchQuery="boxy black jersey t-shirt men",
        searchQueryKo="박시 블랙 저지 티셔츠 남성",
    )


class TestWeakVisionV2:
    def test_strong_item_is_not_weak(self):
        from app.graphs.routing import _is_weak_vision_v2

        assert _is_weak_vision_v2(_make_strong_item()) is False

    def test_rule_1_non_apparel_item_is_weak(self):
        # SPEC-AGENT-V2-CLEANUP-001 — the V1 `_route_after_vision` orchestration
        # was removed (non-apparel now routes to the `agent` node via the
        # inline `_route_after_vision_v2` closure in fashion_bot.py). The
        # preserved, independently-testable contract is the weak-vision
        # predicate: a non-apparel / empty item is weak.
        from app.graphs.routing import _is_weak_vision_v2

        assert _is_weak_vision_v2(None) is True

    def test_rule_2_empty_subcategory_fires(self):
        from app.graphs.routing import _is_weak_vision_v2

        item = _make_strong_item().model_copy(update={"subcategory": ""})
        assert _is_weak_vision_v2(item) is True

    def test_rule_2_ambiguous_subcategory_fires(self, monkeypatch):
        from app.graphs.routing import _is_weak_vision_v2

        item = _make_strong_item().model_copy(update={"subcategory": "thing"})
        assert _is_weak_vision_v2(item) is True

    def test_rule_3_invalid_fit_fires(self):
        from app.graphs.routing import _is_weak_vision_v2

        item = _make_strong_item().model_copy(update={"fit": "stretchy"})
        assert _is_weak_vision_v2(item) is True

    def test_rule_3_empty_fit_fires(self):
        from app.graphs.routing import _is_weak_vision_v2

        item = _make_strong_item().model_copy(update={"fit": ""})
        assert _is_weak_vision_v2(item) is True

    def test_rule_4_empty_color_family_fires(self):
        from app.graphs.routing import _is_weak_vision_v2

        item = _make_strong_item().model_copy(update={"colorFamily": ""})
        assert _is_weak_vision_v2(item) is True

    def test_rule_5_short_search_query_fires(self):
        from app.graphs.routing import _is_weak_vision_v2

        item = _make_strong_item().model_copy(update={"searchQuery": "tee"})
        assert _is_weak_vision_v2(item) is True

    def test_rule_5_threshold_passes_at_min_tokens(self, monkeypatch):
        from app.graphs.routing import _is_weak_vision_v2

        monkeypatch.setattr(settings, "ASK_CLARIFY_MIN_QUERY_TOKENS", 4)
        item = _make_strong_item().model_copy(update={"searchQuery": "boxy black jersey tee"})
        # Exactly 4 tokens = passes (rule fires when < 4).
        assert _is_weak_vision_v2(item) is False


# ── REQ-VISION-SEARCH-001 — ChannelRecommendationRequest backwards compat ──


class TestChannelRecommendationRequestExtension:
    def test_construct_with_legacy_fields_only(self):
        req = ChannelRecommendationRequest(
            image_url="https://x.com/a.jpg",
            item_label="white tee",
            intent="something cheaper",
            keywords=["tee", "white"],
        )
        # Defaults for new fields.
        assert req.item_subcategory is None
        assert req.item_search_query_ko is None
        assert req.outfit_mood_tags == []

    def test_construct_with_rich_fields(self):
        req = ChannelRecommendationRequest(
            image_url="https://x.com/a.jpg",
            item_label="boxy tee",
            intent=None,
            keywords=[],
            item_subcategory="t-shirt",
            item_fit="boxy",
            item_color_family="BLACK",
            item_search_query_en="boxy black jersey t-shirt men",
            item_search_query_ko="박시 블랙 저지 티셔츠 남성",
            outfit_style_node_primary="C",
            outfit_mood_tags=["Street", "Minimal"],
            outfit_gender="male",
        )
        # frozen=True still holds.
        with pytest.raises(Exception):
            req.item_subcategory = "shirt"  # type: ignore[misc]


# ── REQ-VISION-SEARCH-002 — Pipeline maps rich fields into RecommendRequest


class TestPipelineRichMapping:
    @pytest.mark.asyncio
    async def test_rich_fields_reach_recommend_request(self, monkeypatch):
        captured = {}

        from app.models.response import RecommendResponse

        async def _fake_pipeline(req):
            captured["req"] = req
            return RecommendResponse(item_id="x", results=[], counts={}, latency_ms={})

        monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake_pipeline)

        port = PipelineRecommendationPort()
        req = ChannelRecommendationRequest(
            image_url="https://i.pinimg.com/originals/x.jpg",
            item_label="boxy tee",
            intent=None,
            keywords=[],
            item_subcategory="t-shirt",
            item_fit="boxy",
            item_fabric="jersey",
            item_color_family="BLACK",
            item_search_query_en="boxy black jersey t-shirt men",
            item_search_query_ko="박시 블랙 저지 티셔츠 남성",
            outfit_style_node_primary="C",
            outfit_style_node_secondary="D",
            outfit_mood_tags=["Street", "Minimal"],
            outfit_gender="male",
        )
        await port.recommend(req)

        rec = captured["req"]
        # REQ-VISION-SEARCH-002 — searchQueryKo reaches RecommendRequest byte-for-byte.
        assert rec.item.search_query_ko == "박시 블랙 저지 티셔츠 남성"
        assert rec.item.search_query == "boxy black jersey t-shirt men"
        assert rec.item.subcategory == "t-shirt"
        assert rec.item.fit == "boxy"
        assert rec.item.fabric == "jersey"
        assert rec.item.color_family == "BLACK"
        assert rec.gender == "male"
        assert rec.style_node is not None
        assert rec.style_node.primary == "C"
        assert rec.style_node.secondary == "D"
        assert rec.mood_tags == ["Street", "Minimal"]

    @pytest.mark.asyncio
    async def test_legacy_request_unchanged(self, monkeypatch):
        captured = {}

        from app.models.response import RecommendResponse

        async def _fake_pipeline(req):
            captured["req"] = req
            return RecommendResponse(item_id="x", results=[], counts={}, latency_ms={})

        monkeypatch.setattr("app.pipeline.runner.run_pipeline", _fake_pipeline)

        port = PipelineRecommendationPort()
        req = ChannelRecommendationRequest(
            image_url="https://i.pinimg.com/originals/x.jpg",
            item_label="white tee",
            intent="cheaper",
            keywords=["tee", "white"],
        )
        await port.recommend(req)

        rec = captured["req"]
        # No regression — rich fields are None on the legacy path.
        assert rec.item.subcategory is None
        assert rec.item.search_query_ko is None
        assert rec.gender is None
        assert rec.style_node is None


# ── REQ-VISION-COMPAT-003 — legacy session field derivation ────────────────


class TestLegacyDerivation:
    def test_derive_legacy_label_uses_name(self):
        item = _make_strong_item()
        assert derive_legacy_label(item) == "Boxy Tee"

    def test_derive_legacy_label_falls_back_to_subcategory(self):
        item = _make_strong_item().model_copy(update={"name": ""})
        assert derive_legacy_label(item) == "t-shirt"

    def test_derive_legacy_keywords_tokenizes_search_query(self):
        item = _make_strong_item()
        kws = derive_legacy_keywords(item)
        assert "boxy" in kws
        assert "jersey" in kws
        # Lowercased.
        assert all(k == k.lower() for k in kws)

    def test_derive_legacy_keywords_handles_none(self):
        assert derive_legacy_keywords(None) == []
