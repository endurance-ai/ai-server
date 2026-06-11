"""SPEC-CONVERSATION-LOG-001 / LOG-T02 smoke — 21 TypedDicts catalog enforcement.

REQ-LOG-CATALOG-001 acceptance: `__all__` length == 21 (CI gate). Each TypedDict
SHALL be instantiable with the minimum-required keys per SPEC §Event Type Catalog.

Catalog count bumped 19 -> 20 by SPEC-CONVERSATION-LOG-001 v0.3.0 amendment
(cross-SPEC, driven by SPEC-AGENT-V2-REACT REQ-AGENT-LOG-EVENT-001):
`tool_call` is the 20th event type (`ToolCallPayload`).

Catalog count bumped 20 -> 21 by SPEC-DAILY-TOKEN-CAP-001:
`cap_reached` is the 21st event type (`CapReachedPayload`).
"""

from __future__ import annotations

import importlib

import pytest


# Override autouse Docker-dependent fixture from conftest — pure import scan.
@pytest.fixture(autouse=True)
def _truncate_log_table():
    yield


def test_all_exports_exactly_20_typeddicts():
    mod = importlib.import_module("app.observability.event_payloads")
    assert len(mod.__all__) == 23, f"__all__ length must be 23, got {len(mod.__all__)}"


def test_star_import_publishes_20_names():
    """Plan-mandated smoke: `from app.observability.event_payloads import *`."""
    ns: dict = {}
    exec("from app.observability.event_payloads import *", ns)  # noqa: S102
    public = {k for k in ns if not k.startswith("_")}
    assert len(public) == 23, f"star-import exposed {len(public)} names, expected 23"


def test_each_typeddict_instantiable_with_minimum_keys():
    from app.observability.event_payloads import (
        AskClarifySentPayload,
        BotTextPayload,
        CapReachedPayload,
        CardClickedPayload,
        CardSentPayload,
        ClarifyAppliedPayload,
        DiversifyDonePayload,
        EvaluatorRunPayload,
        IntentRoutedPayload,
        LinkResolvedPayload,
        MembershipClickPayload,
        NodeErrorPayload,
        OnboardSelectPayload,
        PickItemDonePayload,
        PinterestIngestPayload,
        SearchDonePayload,
        TasteUpdatePayload,
        ToolCallPayload,
        TurnSummaryPayload,
        UserCallbackPayload,
        UserPhotoPayload,
        UserTextPayload,
        VisionDonePayload,
    )

    # TypedDict instantiation is `dict(...)` at runtime — verify each shape constructs.
    samples: list[dict] = [
        UserTextPayload(text="hi", lang_detected="en"),
        UserPhotoPayload(attachment_id="x", image_url=None, caption=None),
        UserCallbackPayload(callback_data="clarify:fit:slim", source_message_id=1),
        IntentRoutedPayload(intent="new_search_request", critique_delta_summary=None),
        LinkResolvedPayload(input_url="https://pin.it/x", resolved_image_url=None, host="pin.it"),
        VisionDonePayload(schema_v2_used=True, error=None),
        PickItemDonePayload(candidate_items=[], picked_index=0, auto_picked=True),
        AskClarifySentPayload(axis="formality", options_shown=["casual", "formal"]),
        ClarifyAppliedPayload(axis="formality", value="casual", boost_keywords_added=[]),
        SearchDonePayload(top_k_product_ids=[], rrf_scores=[], dense_count=0, sparse_count=0),
        EvaluatorRunPayload(iteration_no=0, score=1.0, delta=None, retry_decision="accept", exhausted=False),
        DiversifyDonePayload(input_count=20, output_count=5, brand_cap=2, platform_cap=3),
        CardSentPayload(product_id="p1", position=0, send_ok=True, send_elapsed_ms=10, source_message_id=42),
        CardClickedPayload(product_id="p1", position=0, dwell_ms=1234),
        OnboardSelectPayload(stage="mood", axis="style", selected_values=["casual"]),
        PinterestIngestPayload(mode="pin", pin_count=1, vision_results_count=1),
        BotTextPayload(chunk_text="hi", chunk_index=0, total_chunks=1, flow="GREETING"),
        TasteUpdatePayload(source="free_text", keywords_delta={}, brands_delta={}),
        NodeErrorPayload(node_name="vision", exception_type="ValueError", message="boom", recovered=False),
        ToolCallPayload(tool_name="search_products", iteration_no=0, latency_ms=120, error=None),
        TurnSummaryPayload(rec_id=None, iter_count=3, status="responded", exit_reason=None),
        CapReachedPayload(lang="ko"),
        MembershipClickPayload(source="cap"),
    ]
    assert len(samples) == 23


def test_all_names_in_module():
    """Every name in __all__ resolves to an actual attribute."""
    mod = importlib.import_module("app.observability.event_payloads")
    for name in mod.__all__:
        assert hasattr(mod, name), f"event_payloads.__all__ lists {name!r} but it is not defined"
