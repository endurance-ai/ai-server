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

    def test_prompt_teaches_distinctive_details(self):
        """detail-extraction PR — the prompt MUST require distinctiveDetails
        and show concrete BAD vs GOOD examples so the model stops defaulting
        to category stereotypes (e.g. labelling a side-button blazer as
        double-breasted)."""
        assert "distinctiveDetails" in ANALYZE_SYSTEM_PROMPT
        assert "side-button" in ANALYZE_SYSTEM_PROMPT
        assert "crew-neck" in ANALYZE_SYSTEM_PROMPT
        # BAD/GOOD framing must be present so the model treats it as a
        # contrast study, not a single isolated example.
        assert "BAD vs GOOD" in ANALYZE_SYSTEM_PROMPT or "❌" in ANALYZE_SYSTEM_PROMPT


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

    def test_vision_item_accepts_distinctive_details(self):
        """detail-extraction PR — VisionItem must round-trip the new
        distinctiveDetails list; default is empty list (backwards-compatible
        when the model omits the field)."""
        item = VisionItem(
            subcategory="blazer",
            distinctiveDetails=["side-button", "notched lapel", "cropped"],
            searchQuery="fitted cropped notched-lapel side-button black wool blazer women",
        )
        assert item.distinctiveDetails == ["side-button", "notched lapel", "cropped"]

        item_default = VisionItem(subcategory="t-shirt")
        assert item_default.distinctiveDetails == []  # legacy payloads still validate


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


class TestMultiLookRetry:
    """B8 — Pinterest multi-look / multi-person photos commonly trigger the
    model's empty-items giveup path. One bounded retry with a multi-look-aware
    user prompt recovers the turn instead of dropping to the hard fallback."""

    @pytest.mark.asyncio
    async def test_empty_items_triggers_multi_look_retry(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "VISION_SCHEMA_V2", True)

        calls = []
        retry_payload = (
            '{"isApparel": true, "styleNode": {"primary": "C", "secondary": "D"}, '
            '"sensitivityTags": [], "mood": {"tags": [], "summary": "", "vibe": "", '
            '"season": "", "occasion": ""}, "palette": [], '
            '"style": {"fit": "regular", "aesthetic": "", "detectedGender": "female"}, '
            '"items": [{"id": "top", "category": "Top", "subcategory": "t-shirt", '
            '"name": "x", "detail": "", "fabric": "jersey", "color": "black", '
            '"colorHex": "#000", "fit": "regular", "colorFamily": "BLACK", '
            '"searchQuery": "black t-shirt women", "searchQueryKo": "", '
            '"position": {"top": 30, "left": 50}}]}'
        )

        async def _chat(*, model, messages, temperature, max_tokens):
            calls.append(messages[1]["content"][0]["text"])
            if len(calls) == 1:
                return {"choices": [{"message": {"content": '{"isApparel": false, "items": []}'}}]}
            return {"choices": [{"message": {"content": retry_payload}}]}

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _chat)

        result = await extract("https://example.com/multilook.jpg")
        assert len(calls) == 2, "retry must fire on empty items"
        assert "multiple people" in calls[1].lower() or "multi" in calls[1].lower()
        assert result.isApparel is True
        assert len(result.items) == 1
        assert result.items[0].subcategory == "t-shirt"

    @pytest.mark.asyncio
    async def test_non_empty_first_pass_does_not_retry(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "VISION_SCHEMA_V2", True)

        calls = []
        good_payload = (
            '{"isApparel": true, "styleNode": {"primary": "C", "secondary": "D"}, '
            '"sensitivityTags": [], "mood": {"tags": [], "summary": "", "vibe": "", '
            '"season": "", "occasion": ""}, "palette": [], '
            '"style": {"fit": "regular", "aesthetic": "", "detectedGender": "male"}, '
            '"items": [{"id": "outer", "category": "Outer", "subcategory": "overcoat", '
            '"name": "y", "detail": "", "fabric": "wool", "color": "grey", '
            '"colorHex": "#888", "fit": "oversized", "colorFamily": "GREY", '
            '"searchQuery": "oversized grey wool overcoat men", "searchQueryKo": "", '
            '"position": {"top": 30, "left": 50}}]}'
        )

        async def _chat(*, model, messages, temperature, max_tokens):
            calls.append(1)
            return {"choices": [{"message": {"content": good_payload}}]}

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _chat)

        result = await extract("https://example.com/good.jpg")
        assert len(calls) == 1, "no retry when first pass already has items"
        assert len(result.items) == 1


# ── B26 — image pre-download to bypass cloud-egress 403 (pinimg) ────────────


class TestImagePreDownload:
    """When the LLM is hosted on a cloud platform (Bedrock) whose IPs are
    blocked by Pinterest CDN, the Vision call dies with 403. Pre-downloading
    the image to bytes from THIS server gives the LLM a data URL it can
    process inline — no server-side fetch from the LLM host."""

    @pytest.mark.asyncio
    async def test_url_input_is_predownloaded_to_bytes(self, monkeypatch):
        """The Vision LLM receives a `data:image/jpeg;base64,…` URL (not the
        original pinimg URL) when pre-download succeeds — proves the URL
        never reaches the model's outbound fetcher."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "VISION_SCHEMA_V2", True)

        # Stub the HTTP client used by `_predownload_image` to return image
        # bytes for the pinimg URL.
        class _FakeResp:
            status_code = 200
            headers = {"content-type": "image/jpeg"}
            content = b"\xff\xd8\xffFAKEJPEG"

        class _FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                return _FakeResp()

        monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

        captured = {}

        async def _chat(*, model, messages, temperature, max_tokens):
            # `messages[1]["content"][1]["image_url"]["url"]` is what the
            # Vision LLM actually sees as the image input.
            captured["image_url"] = messages[1]["content"][1]["image_url"]["url"]
            return {"choices": [{"message": {"content": '{"isApparel": false, "items": []}'}}]}

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _chat)

        await extract("https://i.pinimg.com/originals/ab/61/68/ab61682114846b266ed1a982f9ed5422.jpg")
        # The Vision LLM saw a base64 data URL, NOT the original pinimg URL.
        assert captured["image_url"].startswith("data:image/jpeg;base64,")
        assert "i.pinimg.com" not in captured["image_url"]

    @pytest.mark.asyncio
    async def test_predownload_failure_falls_through_to_url(self, monkeypatch):
        """When pre-download fails (timeout / non-image / network error), the
        original URL falls through so the Vision call still gets a shot — the
        fail-open contract is preserved."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "VISION_SCHEMA_V2", True)

        class _RaisingClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                raise RuntimeError("network down")

        monkeypatch.setattr("httpx.AsyncClient", _RaisingClient)

        captured = {}

        async def _chat(*, model, messages, temperature, max_tokens):
            captured["image_url"] = messages[1]["content"][1]["image_url"]["url"]
            return {"choices": [{"message": {"content": '{"isApparel": false, "items": []}'}}]}

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _chat)

        url = "https://i.pinimg.com/originals/ab/61/68/something.jpg"
        await extract(url)
        # Pre-download blew up → the original URL was passed through.
        assert captured["image_url"] == url

    @pytest.mark.asyncio
    async def test_predownload_skipped_for_data_url(self, monkeypatch):
        """Data URLs (already inline bytes from direct uploads) MUST NOT be
        re-fetched — the URL itself is the payload."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "VISION_SCHEMA_V2", True)

        get_calls = []

        class _Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                get_calls.append(url)
                raise RuntimeError("should not be called")

        monkeypatch.setattr("httpx.AsyncClient", _Client)

        async def _chat(*, model, messages, temperature, max_tokens):
            return {"choices": [{"message": {"content": '{"isApparel": false, "items": []}'}}]}

        monkeypatch.setattr("app.channels.vision.LLMProvider.chat", _chat)

        data_url = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEASA=="
        await extract(data_url)
        assert get_calls == []  # no HTTP fetch attempted


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
        # system content is now a list of cache_control blocks — check the text value.
        sys_content = captured["system"]
        sys_text = sys_content[0]["text"] if isinstance(sys_content, list) else sys_content
        assert "isApparel" in sys_text


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
