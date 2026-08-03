"""Structured node-trace logging helpers (logging-only, zero behavior change).

A tiny shared module so every graph node emits a uniform one-line
enter / done / skip trace at INFO. Extends the existing CLAUDE.md emoji
legend WITHOUT clashing:

  ▶️  [node:<name>] enter
  ✅  [node:<name>] done · k1=v1 k2=v2
  ⏭️  [node:<name>] skip · <reason>

These helpers are pure logging side effects: they take no part in control
flow, return None, and never raise (a logging failure must never break a
graph node). One concise line per transition — no payload bodies, no raw
user text (callers pass only compact structural summaries).

@MX:NOTE: [AUTO] Logging-only trace helper — no behavior, no return value.
@MX:SPEC: SPEC-AGENT-V3-REACT (observability extension)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.graphs.nodes.trace")


def _fmt_summary(summary: dict[str, Any]) -> str:
    """Compact ``k=v`` projection — values stringified + length-capped so a
    stray large value can never blow up a log line."""
    bits: list[str] = []
    for k, v in list(summary.items())[:6]:
        if v is None:
            continue
        sv = str(v)
        if len(sv) > 60:
            sv = sv[:57] + "…"
        bits.append(f"{k}={sv}")
    return " ".join(bits)


def node_enter(name: str) -> None:
    """Log node entry. Never raises."""
    try:
        logger.info("▶️ [node:%s] enter", name)
    except Exception:  # noqa: BLE001 — logging must never break a node
        pass


def node_done(name: str, **summary: Any) -> None:
    """Log normal node exit with 2-4 concise state-delta key/values."""
    try:
        tail = _fmt_summary(summary)
        if tail:
            logger.info("✅ [node:%s] done · %s", name, tail)
        else:
            logger.info("✅ [node:%s] done", name)
    except Exception:  # noqa: BLE001
        pass


def node_skip(name: str, reason: str) -> None:
    """Log a node early-return / no-op with a short reason."""
    try:
        logger.info("⏭️ [node:%s] skip · %s", name, reason)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["node_enter", "node_done", "node_skip"]
