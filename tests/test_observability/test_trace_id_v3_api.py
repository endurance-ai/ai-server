"""REQ-LOG-LANGFUSE-XREF-001 — `current_langfuse_trace_id()` v3 API regression guard.

This test exercises the REAL installed langfuse SDK (NOT a mock). The defect it
guards against was a v2-era API call (`get_current_observation().trace_id` +
`langfuse.langfuse_context`) that does not exist in langfuse v3, so the helper
silently always returned None. A mocked test would not catch that class of bug —
the whole point is to run against the genuine v3 surface so the next SDK
migration cannot silently regress this again.

A real Langfuse client is created with dummy keys + `tracing_enabled=True` and
its `start_as_current_span()` establishes the active OTEL context that the app
helper reads via the real v3 `client.get_current_trace_id()`. No server is
required: trace-id resolution is OTEL-context-local and works fully offline (the
background span exporter failing to reach the host is irrelevant and only emits
stderr noise).

NOTE: this uses the explicit-client `start_as_current_span()` rather than the
`@observe` decorator on purpose — pytest runs multiple langfuse clients in one
process, and the v3 `@observe` cross-project-leakage guard skips span creation
for an undecorated public key in that scenario. `start_as_current_span()` on the
explicit client is the SDK's context-capable mode and produces the same active
trace that production resolves through `client.get_current_trace_id()`.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_trace_id_resolves_inside_real_span_and_none_outside() -> None:
    try:
        from langfuse import Langfuse
    except Exception as exc:  # noqa: BLE001 — SDK genuinely unavailable
        pytest.skip(f"langfuse v3 SDK not importable: {exc!r}")

    from app.observability import langfuse as obs

    # Real (non-mocked) v3 client. Dummy keys + tracing_enabled=True is enough:
    # get_current_trace_id() is OTEL-context-local and needs no live server.
    client = Langfuse(
        public_key="pk-test",
        secret_key="sk-test",
        host="http://localhost:1",
        tracing_enabled=True,
    )

    # Point the app helper at this REAL SDK client (not a mock) so the genuine
    # v3 get_current_trace_id() path is what's under test, regardless of whether
    # LANGFUSE_* keys were set at import time. Restored afterwards so module
    # state does not leak into other tests.
    _orig_get_client = obs._lf_get_client
    obs._lf_get_client = lambda: client  # type: ignore[assignment]
    try:

        async def _deep_call() -> str | None:
            # Mirror the production path: resolution happens inside a nested
            # await within an active span.
            await asyncio.sleep(0)
            return obs.current_langfuse_trace_id()

        with client.start_as_current_span(name="node.agent"):
            inside = await _deep_call()

        assert isinstance(inside, str) and inside, (
            f"expected a real trace id inside an active span, got {inside!r} "
            "(v2/v3 API regression — helper is not resolving the v3 trace)"
        )

        # No active span → must be None (best-effort contract preserved).
        outside = obs.current_langfuse_trace_id()
        assert outside is None, f"expected None with no active span, got {outside!r}"
    finally:
        obs._lf_get_client = _orig_get_client  # type: ignore[assignment]
        # Shut the real client down so its background OTEL exporter does not
        # flush/retry after pytest closes the log stream (else a post-session
        # "I/O operation on closed file" traceback pollutes CI output + ~5s wait).
        try:
            client.shutdown()
        except Exception:
            pass
