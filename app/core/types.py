"""Shared internal type aliases (SPEC-ARCH-AI-001 PR5, REQ-AI-005).

Framework-agnostic primitive aliases used by the domain layer. These are
NOT Pydantic transport DTOs (those stay in app/models/); keeping the two
separate lets internal business types and wire schemas evolve independently
(REQ-AI-005). Behavior byte-identical — this is additive scaffolding only;
the runner serialization path (Net(1)) is untouched.
"""

from __future__ import annotations

from typing import Any

# A raw RPC row as returned by search_products_v5 (pre-domain-mapping).
RpcRow = dict[str, Any]

# Stage -> count / latency-ms measurement maps (mirrors PipelineState).
CountMap = dict[str, int]
LatencyMap = dict[str, int]

__all__ = ["RpcRow", "CountMap", "LatencyMap"]
