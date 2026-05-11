"""SPEC-OBSERVABILITY-002 / REQ-OBS-COST-001 — latency overhead benchmark.

Measures the per-webhook latency delta between Langfuse-disabled (keys unset)
and Langfuse-enabled (keys set, decorators active) configurations of the SAME
post-SPEC binary. The goal is < 5ms p99 added overhead.

This is a smoke benchmark, not a CI gate — it requires a running event loop
and synthetic state. Run manually:

    uv run python scripts/bench_observability.py

The script does NOT contact a real Langfuse server — the SDK enqueues spans to
its internal background queue, which is the hot-path cost we care about. The
actual network send is asynchronous and out of scope for this measurement.

Output: prints p50/p95/p99 for both configurations + the delta.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

# Allow running directly via `uv run python scripts/...`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ITERATIONS = 1000


async def _noop_node(state: dict) -> dict:
    """Synthetic node — represents the minimum work each @observe wraps."""
    return {"ok": True, "size": len(state)}


async def _run_once(decorated: bool) -> int:
    """Returns wall-clock ns for one invocation."""
    from app.observability.langfuse import observe

    if decorated:

        @observe(name="node.bench", as_type="span")
        async def fn(state):
            return await _noop_node(state)

    else:

        async def fn(state):
            return await _noop_node(state)

    state = {"i": 0, "msg": "synthetic"}
    t0 = time.perf_counter_ns()
    await fn(state)
    return time.perf_counter_ns() - t0


async def _bench(n: int) -> list[int]:
    samples = []
    for _ in range(n):
        samples.append(await _run_once(decorated=True))
    return samples


async def main() -> None:
    # Warm-up
    await _bench(50)

    # Configuration 1: keys unset → cascade collapses to no-op
    os.environ["LANGFUSE_PUBLIC_KEY"] = ""
    os.environ["LANGFUSE_SECRET_KEY"] = ""
    # Reload module so `_ENABLED` re-evaluates
    import importlib

    from app.observability import langfuse as obs_mod

    importlib.reload(obs_mod)
    baseline = await _bench(ITERATIONS)

    # Configuration 2: keys set → real v3 SDK active
    os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-bench"
    os.environ["LANGFUSE_SECRET_KEY"] = "sk-bench"
    importlib.reload(obs_mod)
    enabled = await _bench(ITERATIONS)

    def p(samples, q):
        s = sorted(samples)
        idx = int(q * len(s))
        return s[min(idx, len(s) - 1)]

    print(f"baseline (no-op) — p50={p(baseline, 0.50) / 1000:.1f}us, p99={p(baseline, 0.99) / 1000:.1f}us")
    print(f"enabled  (v3)   — p50={p(enabled, 0.50) / 1000:.1f}us, p99={p(enabled, 0.99) / 1000:.1f}us")
    delta_p99_ms = (p(enabled, 0.99) - p(baseline, 0.99)) / 1_000_000
    print(f"delta p99 = {delta_p99_ms:.3f} ms (budget: < 5.0 ms)")
    print(f"median delta = {(statistics.median(enabled) - statistics.median(baseline)) / 1000:.1f} us")


if __name__ == "__main__":
    asyncio.run(main())
