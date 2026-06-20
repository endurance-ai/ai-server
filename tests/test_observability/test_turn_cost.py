"""Unit tests for the per-turn LLM cost accumulator (audit: total-cost tracking).

Covers the source breakdown, unknown-model handling, cache-creation token
accounting, and the per-source sum == turn total invariant.
"""

from __future__ import annotations

import contextvars

from app.observability import turn_cost


def test_no_frame_is_noop_and_returns_zero():
    """accumulate_* before reset_turn() must silently no-op (e.g. unframed call).

    Run in a fresh empty Context so a reset_turn() leaked from another test
    cannot make the accumulator's ContextVar appear set.
    """

    def body():
        turn_cost.accumulate_raw("nova-lite", {"prompt_tokens": 100, "completion_tokens": 10}, source="vision")
        return turn_cost.get_turn_totals()

    totals = contextvars.Context().run(body)
    assert totals["cost_usd"] == 0.0
    assert totals["by_source"] == {}


def test_raw_accumulation_records_source_and_cost():
    turn_cost.reset_turn()
    turn_cost.accumulate_raw(
        "nova-lite",
        {"prompt_tokens": 1000, "completion_tokens": 500},
        source="vision",
    )
    totals = turn_cost.get_turn_totals()
    # nova-lite: input 0.060 / output 0.240 per 1M
    expected = (1000 * 0.060 + 500 * 0.240) / 1_000_000
    assert abs(totals["cost_usd"] - expected) < 1e-12
    assert totals["input_tokens"] == 1000
    assert totals["output_tokens"] == 500
    assert "vision" in totals["by_source"]
    assert totals["by_source"]["vision"]["calls"] == 1
    assert totals["by_source"]["vision"]["cost_known"] is True


def test_lc_accumulation_with_cache_creation_tokens():
    turn_cost.reset_turn()
    turn_cost.accumulate_lc(
        "claude-haiku-4-5",
        {
            "input_tokens": 2000,
            "output_tokens": 300,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 400,
        },
        source="react_loop",
    )
    totals = turn_cost.get_turn_totals()
    # cache_creation must be tracked as its own token counter (finding #6).
    assert totals["cache_creation_tokens"] == 400
    assert totals["cache_read_tokens"] == 500
    # cost: plain_inp = 2000 - 500 - 400 = 1100
    r = {"input": 1.000, "output": 5.000, "cache_read": 0.1000, "cache_creation": 1.250}
    expected = (1100 * r["input"] + 300 * r["output"] + 500 * r["cache_read"] + 400 * r["cache_creation"]) / 1_000_000
    assert abs(totals["cost_usd"] - expected) < 1e-12


def test_unknown_model_counts_tokens_but_zero_cost(caplog):
    turn_cost.reset_turn()
    turn_cost.accumulate_raw(
        "some-brand-new-model-v9",
        {"prompt_tokens": 1000, "completion_tokens": 200},
        source="router",
    )
    totals = turn_cost.get_turn_totals()
    assert totals["cost_usd"] == 0.0  # finding #5: silent $0 must be loud, not crash
    assert totals["total_tokens"] == 1200  # tokens still counted
    assert totals["by_source"]["router"]["cost_known"] is False


def test_per_source_sum_equals_turn_total():
    turn_cost.reset_turn()
    turn_cost.accumulate_raw("nova-lite", {"prompt_tokens": 1000, "completion_tokens": 100}, source="vision")
    turn_cost.accumulate_raw(
        "claude-haiku-4-5", {"prompt_tokens": 500, "completion_tokens": 50}, source="intent_classifier"
    )
    turn_cost.accumulate_lc("nova-lite", {"input_tokens": 800, "output_tokens": 200}, source="react_loop")
    totals = turn_cost.get_turn_totals()
    source_sum = sum(b["cost_usd"] for b in totals["by_source"].values())
    assert abs(source_sum - totals["cost_usd"]) < 1e-12
    assert set(totals["by_source"]) == {"vision", "intent_classifier", "react_loop"}


def test_mark_summary_emitted_flag():
    turn_cost.reset_turn()
    assert turn_cost.get_turn_totals()["summary_emitted"] is False
    turn_cost.mark_summary_emitted()
    assert turn_cost.get_turn_totals()["summary_emitted"] is True


def test_get_turn_totals_by_source_is_isolated_copy():
    """Mutating the returned snapshot must not corrupt the live accumulator."""
    turn_cost.reset_turn()
    turn_cost.accumulate_raw("nova-lite", {"prompt_tokens": 100, "completion_tokens": 10}, source="vision")
    snap = turn_cost.get_turn_totals()
    snap["by_source"]["vision"]["cost_usd"] = 999.0
    again = turn_cost.get_turn_totals()
    assert again["by_source"]["vision"]["cost_usd"] != 999.0
