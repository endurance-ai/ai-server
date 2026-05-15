"""SPEC-AGENT-V2-REACT / REQ-AGENT-COMPAT-STATE-001 — WorkingState +3 fields."""

from __future__ import annotations

from app.graphs.state import WorkingState


def _existing_fields() -> set[str]:
    # Snapshot of fields PRESENT BEFORE SPEC-AGENT-V2-REACT — used to verify
    # no existing field was removed or renamed (REQ-AGENT-COMPAT-STATE-001 AC).
    return {
        "message",
        "chat_id",
        "from_user_id",
        "thread_id",
        "turn_no",
        "decision",
        "image_url",
        "detected_items",
        "selected_item_index",
        "critique_delta",
        "candidates",
        "sent_candidates",
        "response_text",
        "presearch_summary",
        "vision_result",
        "vision_selected_item",
        "vision_outfit_style_node_primary",
        "vision_outfit_style_node_secondary",
        "vision_outfit_mood_tags",
        "vision_outfit_gender",
        "critique_retry_count",
        "critique_trail",
        "critique_pending_delta",
        "critique_exhausted",
        "critique_started_at_ms",
        "critique_previous_candidates",
        "critique_previous_score",
        "clarify_axis",
        "clarify_value",
        "clarify_delta",
        "messages",
        "log_events",
        "onboard_stage",
        "onboard_selections",
        "onboard_card_message_id",
        "continuous_origin",
        "onboard_pin_weights",
    }


def test_3_new_fields_only():
    fields = set(WorkingState.model_fields.keys())
    new_fields = {"agent_iterations", "tool_call_history", "agent_status"}
    assert new_fields.issubset(fields), f"missing new fields: {new_fields - fields}"


def test_existing_fields_unchanged():
    fields = set(WorkingState.model_fields.keys())
    expected_existing = _existing_fields()
    missing = expected_existing - fields
    assert not missing, f"removed pre-existing fields: {missing}"


def test_agent_status_default_running():
    from datetime import UTC, datetime

    from app.channels.schemas import ChannelMessage

    msg = ChannelMessage(chat_id=1, text="hi", received_at=datetime.now(UTC))
    ws = WorkingState(message=msg, chat_id=1)
    assert ws.agent_status == "running"
    assert ws.agent_iterations == 0
    assert ws.tool_call_history == []
