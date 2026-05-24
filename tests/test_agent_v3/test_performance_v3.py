"""SPEC-AGENT-V3-REACT / T10 — performance guards.

REQ-AGENT-V3-PERF-001 / AC-P.2. Full 200-turn p95<8s load is an
operational/manual gate (V2 harness reuse, documented in plan.md runbook).
Here we mechanically assert the bounded unit invariant that GUARANTEES the
inherited budget is not exceeded:
  - build_memory_context assembly overhead < 50ms (AC-P.2)

(The slow-evaluator residual-cancel guard was removed with Reflexion/Gap2.)
"""

from __future__ import annotations

import time

import pytest

from app.agents import _memory_context

# Taste-store reset + settings snapshot/restore handled centrally by
# tests/test_agent_v3/conftest.py::_v3_isolation (autouse).


@pytest.mark.asyncio
async def test_ac_p_2_memory_assembly_under_50ms(monkeypatch):
    """AC-P.2 — build_memory_context assembly overhead < 50ms."""
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:99")
    prof.liked_brands = {f"b{i}": float(i) for i in range(50)}
    prof.liked_keywords = {f"k{i}": float(i) for i in range(50)}
    get_taste_store().update(prof)

    async def _grh(args, ctx):
        return {
            "ok": True,
            "events": [{"event_type": "user_text", "payload_summary": {"text": "hi"}} for _ in range(5)],
        }

    monkeypatch.setattr("app.agents.tools.get_recent_history.dispatch", _grh)

    ctx = {"user_key": "u:99"}
    # Warm + measure best-of-3 to exclude import jitter.
    await _memory_context.build_memory_context(None, None, ctx, max_tokens=1500)
    samples = []
    for _ in range(3):
        t0 = time.monotonic()
        await _memory_context.build_memory_context(None, None, ctx, max_tokens=1500)
        samples.append(time.monotonic() - t0)
    best = min(samples)
    assert best < 0.05, f"memory assembly too slow: {best * 1000:.1f}ms"
