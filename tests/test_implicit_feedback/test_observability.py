"""REQ-FB-OBSERVABILITY-001 — span emission + PII safety."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_log_impressions_emits_span_metadata():
    from app.channels import implicit_feedback as ifb

    ifb.set_backend_is_postgres(False)  # avoid pool; verify span only
    captured: list[dict] = []

    def _spy(metadata=None, **kw):
        captured.append(metadata or {})

    with patch.object(ifb, "update_current_span", _spy):
        await ifb.log_impressions(987654321, None, [])

    assert captured
    md = captured[-1]
    assert md["count"] == 0
    assert md["backend"] == "in_memory_skipped"
    assert md["chat_id_hash"] and md["chat_id_hash"] != "987654321"


@pytest.mark.asyncio
async def test_click_span_metadata_keys():
    from app.channels import implicit_feedback as ifb

    ifb.set_backend_is_postgres(False)
    captured: list[dict] = []

    def _spy(metadata=None, **kw):
        captured.append(metadata or {})

    with patch.object(ifb, "update_current_span", _spy):
        await ifb.record_click(123, None, "p1", "ami", ["a"])

    md = captured[-1]
    for key in ("chat_id_hash", "product_id", "brand", "weight", "stale", "db_rows_affected"):
        assert key in md
    assert md["product_id"] == "p1"
    assert md["weight"] == 1.0
    assert md["stale"] is False


@pytest.mark.asyncio
async def test_no_raw_chat_id_in_span_payload():
    """PII: raw chat_id (999999999) MUST NOT appear in span metadata."""
    from app.channels import implicit_feedback as ifb

    ifb.set_backend_is_postgres(False)
    captured: list[dict] = []

    def _spy(metadata=None, **kw):
        captured.append(metadata or {})

    with patch.object(ifb, "update_current_span", _spy):
        await ifb.log_impressions(999999999, None, [])
        await ifb.record_click(999999999, None, "p", "b", ["k"])
        await ifb.attribute_expired_impressions(999999999, None)

    for md in captured:
        serialized = repr(md)
        assert "999999999" not in serialized


@pytest.mark.asyncio
async def test_re_query_triggered_false_span(fake_candidates):
    from app.channels import implicit_feedback as ifb
    from app.channels.session import Session, SessionState

    captured: list[dict] = []

    def _spy(metadata=None, **kw):
        captured.append(metadata or {})

    sess = Session(chat_id=42, state=SessionState.IDLE, last_results=[])
    with patch.object(ifb, "update_current_span", _spy):
        await ifb.detect_and_apply_re_query(sess, inbound_is_fresh_query=True)

    md = captured[-1]
    assert md["triggered"] is False
    assert md["products_count"] == 0
