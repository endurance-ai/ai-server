"""Per-call-site source labelling on the turn-cost ledger.

The ledger (feature/turn-cost-ledger) records one `llm_call` row per LLM call.
This test pins the call-site `source` label (vision / evaluator / ...) and the
`transport` discriminator so each expenditure stays attributable to its origin.
"""

from __future__ import annotations

import contextvars

from app.observability import turn_cost


def test_raw_records_call_site_source_and_transport():
    turn_cost.reset_turn()
    turn_cost.accumulate_raw(
        "nova-lite",
        {"prompt_tokens": 1000, "completion_tokens": 100},
        source="vision",
    )
    calls = turn_cost.get_turn_totals()["calls"]
    assert len(calls) == 1
    assert calls[0]["source"] == "vision"
    assert calls[0]["transport"] == "raw"


def test_lc_records_call_site_source_and_transport():
    turn_cost.reset_turn()
    turn_cost.accumulate_lc("nova-lite", {"input_tokens": 800, "output_tokens": 200}, source="react_loop")
    calls = turn_cost.get_turn_totals()["calls"]
    assert calls[0]["source"] == "react_loop"
    assert calls[0]["transport"] == "langchain"


def test_default_source_falls_back_to_transport_name():
    turn_cost.reset_turn()
    turn_cost.accumulate_raw("nova-lite", {"prompt_tokens": 10, "completion_tokens": 1})
    turn_cost.accumulate_lc("nova-lite", {"input_tokens": 10, "output_tokens": 1})
    calls = turn_cost.get_turn_totals()["calls"]
    assert calls[0]["source"] == "raw"
    assert calls[1]["source"] == "langchain"


def test_distinct_sources_are_individually_attributable():
    """Same model from different call-sites must remain separable by source."""
    turn_cost.reset_turn()
    turn_cost.accumulate_raw("nova-lite", {"prompt_tokens": 100, "completion_tokens": 10}, source="vision")
    turn_cost.accumulate_raw("nova-lite", {"prompt_tokens": 100, "completion_tokens": 10}, source="evaluator")
    calls = turn_cost.get_turn_totals()["calls"]
    by_source = {c["source"]: c for c in calls}
    assert set(by_source) == {"vision", "evaluator"}
    # both nova-lite, but the ledger keeps them as separate identifiable rows
    assert by_source["vision"]["cost_usd"] > 0
    assert by_source["evaluator"]["cost_usd"] > 0


def test_no_frame_is_noop():
    def body():
        turn_cost.accumulate_raw("nova-lite", {"prompt_tokens": 100, "completion_tokens": 10}, source="vision")
        return turn_cost.get_turn_totals()

    totals = contextvars.Context().run(body)
    assert totals["cost_usd"] == 0.0
    assert totals["calls"] == []
