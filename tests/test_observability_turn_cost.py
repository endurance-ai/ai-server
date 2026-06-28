from __future__ import annotations

from uuid import uuid4

from app.observability import turn_cost


def test_accumulate_raw_prefers_litellm_response_cost(monkeypatch):
    emitted: list[dict] = []

    def fake_emit(**kwargs):
        emitted.append(kwargs)

    monkeypatch.setattr("app.observability.conversation_log.emit", fake_emit)
    monkeypatch.setattr("app.observability.langfuse.current_langfuse_trace_id", lambda: "trace-1")

    thread_id = uuid4()
    turn_cost.reset_turn(
        turn_id=f"{thread_id}:3",
        user_key="u1",
        chat_id=42,
        thread_id=thread_id,
        turn_no=3,
    )

    turn_cost.accumulate_raw(
        "bedrock/us.anthropic.claude-haiku-4-5",
        {"input_tokens": 1000, "output_tokens": 100},
        response={"_hidden_params": {"response_cost": 0.123456789}},
    )

    totals = turn_cost.get_turn_totals()
    assert totals["cost_usd"] == 0.123456789
    assert totals["total_tokens"] == 1100
    assert totals["calls"][0]["cost_source"] == "litellm"
    assert totals["calls"][0]["turn_id"] == f"{thread_id}:3"
    assert emitted[0]["event_type"] == "llm_call"
    assert emitted[0]["payload"]["cost_usd"] == 0.123456789
    assert emitted[0]["payload"]["langfuse_trace"] == "trace-1"


def test_accumulate_lc_records_fallback_cost_and_metadata():
    turn_cost.reset_turn(turn_id="turn-1")

    turn_cost.accumulate_lc(
        "claude-haiku-4-5",
        {
            "input_tokens": 1000,
            "output_tokens": 100,
            "total_tokens": 1100,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 100,
        },
    )

    totals = turn_cost.get_turn_totals()
    assert round(totals["cost_usd"], 7) == 0.001075
    assert totals["cache_read_tokens"] == 500
    assert totals["calls"][0]["source"] == "langchain"
    assert totals["calls"][0]["cost_source"] == "fallback_rates"
    assert turn_cost.langfuse_metadata() == {"turn_id": "turn-1"}


def test_unframed_raw_call_still_emits_llm_call(monkeypatch):
    emitted: list[dict] = []

    def fake_emit(**kwargs):
        emitted.append(kwargs)

    monkeypatch.setattr("app.observability.conversation_log.emit", fake_emit)
    turn_cost.clear_turn()

    turn_cost.accumulate_raw(
        "nova-lite",
        {"prompt_tokens": 100, "completion_tokens": 10},
        source="enhance_query",
    )

    assert emitted[0]["event_type"] == "llm_call"
    assert emitted[0]["user_key"] == "internal:unframed"
    assert emitted[0]["chat_id"] == 0
    assert emitted[0]["payload"]["source"] == "enhance_query"
    assert emitted[0]["payload"]["framed"] is False
