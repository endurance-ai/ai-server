from __future__ import annotations

from app.observability import langfuse


class _FakeClient:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def update_current_trace(self, **kwargs):
        self.updates.append(kwargs)


def test_add_trace_event_appends_readable_conversation_flow(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(langfuse, "_lf_get_client", lambda: client)
    langfuse.reset_trace_story()

    langfuse.add_trace_event("user_text", {"text": "추천해줘\n검정 코트", "lang_detected": "ko"})
    langfuse.add_trace_event("tool_call", {"tool_name": "search_products", "iteration_no": 1, "latency_ms": 42})
    langfuse.add_trace_event(
        "llm_call",
        {
            "model": "claude-haiku-4-5",
            "total_tokens": 1234,
            "cost_usd": 0.0012,
            "cost_source": "litellm",
        },
    )

    metadata = client.updates[-1]["metadata"]
    assert metadata["conversation_event_count"] == 3
    assert metadata["conversation_flow"][0] == {
        "idx": 1,
        "type": "user_text",
        "text": "추천해줘 검정 코트",
        "lang": "ko",
    }
    assert metadata["conversation_flow"][1]["tool"] == "search_products"
    assert metadata["conversation_last_event"] == {
        "idx": 3,
        "type": "llm_call",
        "model": "claude-haiku-4-5",
        "tokens": 1234,
        "cost_usd": 0.0012,
        "cost_source": "litellm",
    }


def test_reset_trace_story_starts_new_flow(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(langfuse, "_lf_get_client", lambda: client)
    langfuse.reset_trace_story()

    langfuse.add_trace_event("bot_text", {"chunk_text": "첫 번째", "chunk_index": 1, "total_chunks": 1})
    langfuse.reset_trace_story()
    langfuse.add_trace_event("turn_summary", {"status": "responded", "total_tokens": 10, "cost_usd": 0.2})

    metadata = client.updates[-1]["metadata"]
    assert metadata["conversation_event_count"] == 1
    assert metadata["conversation_flow"] == [
        {"idx": 1, "type": "turn_summary", "status": "responded", "tokens": 10, "cost_usd": 0.2}
    ]
