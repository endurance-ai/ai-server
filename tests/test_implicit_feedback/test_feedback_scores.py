"""P0 user-feedback scores — retro-scoring the ORIGINAL recommendation trace.

Two layers:
  1. Pure unit tests of `emit_feedback_score` (no Docker) — gating, signal
     mapping, fail-open, disabled no-op.
  2. Docker-gated integration: log_impressions binds the original trace id to
     the impression row; record_click / attribute_expired_impressions read it
     back and score that ORIGINAL trace (not the click-time trace).
"""

from __future__ import annotations

import pytest

from tests.test_implicit_feedback.conftest import _DOCKER_OK

# ── Layer 1: pure unit tests of the helper (no Postgres / no Docker) ───────


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_score(self, **kwargs) -> None:
        self.calls.append(kwargs)


def _patch_client(monkeypatch, client) -> None:
    import app.observability.langfuse as lf

    monkeypatch.setattr(lf, "_lf_get_client", (lambda: client) if client is not None else None)
    monkeypatch.setattr(lf.settings, "LANGFUSE_FEEDBACK_SCORES", True, raising=False)


def test_click_emits_single_positive_score(monkeypatch):
    from app.observability.langfuse import emit_feedback_score

    client = _FakeClient()
    _patch_client(monkeypatch, client)

    emit_feedback_score("trace-ORIG", signal="click", product_id="p2", brand="acne")

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["trace_id"] == "trace-ORIG"
    assert call["name"] == "user_feedback"
    assert call["value"] == 1.0
    assert call["data_type"] == "NUMERIC"
    assert "product_id=p2" in call["comment"]
    assert "brand=acne" in call["comment"]


def test_no_click_emits_zero_score(monkeypatch):
    from app.observability.langfuse import emit_feedback_score

    client = _FakeClient()
    _patch_client(monkeypatch, client)

    emit_feedback_score("trace-ORIG", signal="no_click", brand="ami")

    assert len(client.calls) == 1
    assert client.calls[0]["name"] == "user_feedback"
    assert client.calls[0]["value"] == 0.0


def test_re_query_emits_dual_score(monkeypatch):
    from app.observability.langfuse import emit_feedback_score

    client = _FakeClient()
    _patch_client(monkeypatch, client)

    emit_feedback_score("trace-ORIG", signal="re_query", attribution_window_s=90)

    names = {(c["name"], c["value"]) for c in client.calls}
    assert ("user_feedback", 0.0) in names
    assert ("re_query", 1.0) in names
    assert all(c["trace_id"] == "trace-ORIG" for c in client.calls)
    assert any("attribution_window_s=90" in c["comment"] for c in client.calls)


def test_disabled_langfuse_is_noop(monkeypatch):
    from app.observability.langfuse import emit_feedback_score

    _patch_client(monkeypatch, None)
    # Must not raise even though there is no client at all.
    emit_feedback_score("trace-ORIG", signal="click")


def test_kill_switch_off_is_noop(monkeypatch):
    import app.observability.langfuse as lf
    from app.observability.langfuse import emit_feedback_score

    client = _FakeClient()
    _patch_client(monkeypatch, client)
    monkeypatch.setattr(lf.settings, "LANGFUSE_FEEDBACK_SCORES", False, raising=False)

    emit_feedback_score("trace-ORIG", signal="click")
    assert client.calls == []


def test_missing_trace_id_is_noop(monkeypatch):
    from app.observability.langfuse import emit_feedback_score

    client = _FakeClient()
    _patch_client(monkeypatch, client)

    emit_feedback_score(None, signal="click")
    emit_feedback_score("", signal="click")
    assert client.calls == []


def test_unknown_signal_is_noop(monkeypatch):
    from app.observability.langfuse import emit_feedback_score

    client = _FakeClient()
    _patch_client(monkeypatch, client)

    emit_feedback_score("trace-ORIG", signal="hover")
    assert client.calls == []


def test_scoring_failure_never_propagates(monkeypatch):
    from app.observability.langfuse import emit_feedback_score

    class _BoomClient:
        def create_score(self, **kwargs):
            raise RuntimeError("langfuse down")

    _patch_client(monkeypatch, _BoomClient())
    # Swallowed + logged at WARNING — no exception escapes.
    emit_feedback_score("trace-ORIG", signal="click")


# ── Layer 2: Docker-gated integration (original-trace attribution) ─────────

pytestmark_pg = pytest.mark.skipif(not _DOCKER_OK, reason="Docker unavailable — pg integration skipped")


@pytestmark_pg
@pytest.mark.asyncio
async def test_click_scores_original_recommendation_trace(
    pg_pool_with_impressions, fake_candidates, in_memory_taste_store, monkeypatch
):
    """log_impressions captures trace ORIG; a later record_click (different
    webhook = different live trace) must score ORIG, not the click trace."""
    import app.channels.implicit_feedback as ifb

    client = _FakeClient()
    monkeypatch.setattr(ifb, "emit_feedback_score", _real_emit_with(client, monkeypatch))

    # Impression webhook context: live trace = "trace-ORIG"
    monkeypatch.setattr(ifb, "current_langfuse_trace_id", lambda: "trace-ORIG")
    await ifb.log_impressions(41, None, fake_candidates)

    # Click webhook context: live trace would be "trace-CLICK" (must NOT be used)
    monkeypatch.setattr(ifb, "current_langfuse_trace_id", lambda: "trace-CLICK")
    rows = await ifb.record_click(41, None, "p2", "acne", ["wide"])
    assert rows == 1

    assert len(client.calls) == 1
    assert client.calls[0]["trace_id"] == "trace-ORIG"
    assert client.calls[0]["value"] == 1.0


@pytestmark_pg
@pytest.mark.asyncio
async def test_no_click_expiry_scores_original_trace(
    pg_pool_with_impressions, fake_candidates, in_memory_taste_store, monkeypatch
):
    import app.channels.implicit_feedback as ifb
    from app.providers.db_pool import get_pool, run_in_pool_loop

    client = _FakeClient()
    monkeypatch.setattr(ifb, "emit_feedback_score", _real_emit_with(client, monkeypatch))
    monkeypatch.setattr(ifb, "current_langfuse_trace_id", lambda: "trace-ORIG")

    await ifb.log_impressions(42, None, fake_candidates)

    async def _expire():
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE ai.card_impression SET shown_at = now() - interval '1200 seconds' WHERE chat_id=%s",
                (42,),
            )
            await conn.commit()

    run_in_pool_loop(_expire())

    n = await ifb.attribute_expired_impressions(42, None)
    assert n == 3
    assert len(client.calls) == 3
    assert all(
        c["trace_id"] == "trace-ORIG" and c["name"] == "user_feedback" and c["value"] == 0.0 for c in client.calls
    )


def _real_emit_with(client, monkeypatch):
    """Bind the real emit_feedback_score to a fake Langfuse client so the
    integration path exercises the genuine helper, not a stub."""
    import app.observability.langfuse as lf

    monkeypatch.setattr(lf, "_lf_get_client", lambda: client)
    monkeypatch.setattr(lf.settings, "LANGFUSE_FEEDBACK_SCORES", True, raising=False)
    return lf.emit_feedback_score
