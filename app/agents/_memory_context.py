"""SPEC-AGENT-V3-REACT / Gap1 — auto memory context builder.

@MX:NOTE: [AUTO] WRAP-ONLY — composes existing helpers, defines NO new memory
  or summarization algorithm. Wraps:
  - app.channels.taste_profile.get_taste_store().get_or_create + TasteProfile
    .boost_brands / .boost_keywords / .exclude_brands
  - app.agents.tools.get_recent_history.dispatch (its own SELECT +
    _summarize_payload 200-char cap is the payload-truncation policy reused)
@MX:SPEC: SPEC-AGENT-V3-REACT

Produces a single system-derived string fenced by
`[MEMORY CONTEXT — SYSTEM DERIVED]` … `[/MEMORY CONTEXT]`. Called ONLY from
`run_react_loop` when `AGENT_V3_MEMORY_INJECTION_ENABLED` is true; with the
flag off this module is never imported (dead code) so V2 messages stay
byte-identical (REQ-AGENT-V3-MEM-FLAG-001).

Sizing (REQ-AGENT-V3-MEM-CAP-001): the whole block is capped at
`max_tokens * 4` chars. Taste summary is preserved first, then recent turns
newest-first; older turns are dropped when the budget is exhausted.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_FENCE_OPEN = "[MEMORY CONTEXT — SYSTEM DERIVED]"
_FENCE_CLOSE = "[/MEMORY CONTEXT]"
_EMPTY_PLACEHOLDER = "(no taste history yet)"
# REQ-AGENT-V3-MEM-INJECT-001 — recent-N default (OQ-V3-4 resolved = 5).
_RECENT_N = 5


def _taste_lines(user_key: str) -> list[str]:
    """Taste summary via existing TasteProfile boost/exclude helpers only."""
    try:
        from app.channels.taste_profile import get_taste_store

        store = get_taste_store()
        profile = store.get_or_create(user_key)
    except Exception as exc:  # noqa: BLE001 — fail-soft, never break the loop
        logger.debug("[_memory_context] taste store failed: %r", exc)
        return []

    liked_brands = profile.boost_brands(5)
    liked_keywords = profile.boost_keywords(5)
    disliked_brands = profile.exclude_brands()
    lines: list[str] = []
    if liked_brands:
        lines.append(f"liked_brands: {', '.join(liked_brands)}")
    if liked_keywords:
        lines.append(f"liked_keywords: {', '.join(liked_keywords)}")
    if disliked_brands:
        lines.append(f"disliked_brands: {', '.join(disliked_brands)}")
    return lines


async def _recent_lines(ctx: dict[str, Any]) -> list[str]:
    """Recent-turn summary via the EXISTING get_recent_history dispatch.

    Its `_summarize_payload` already applies the 200-char-per-item cap
    (SPEC-CONVERSATION-LOG-001 payload-truncation policy) — reused, not
    reimplemented. Returned newest-first (dispatch ORDER BY ts DESC).
    """
    try:
        from app.agents.tools.get_recent_history import dispatch as grh_dispatch

        res = await grh_dispatch({"n": _RECENT_N}, ctx)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[_memory_context] get_recent_history failed: %r", exc)
        return []

    events = res.get("events") if isinstance(res, dict) else None
    if not events:
        return []
    lines: list[str] = []
    for ev in events:
        et = ev.get("event_type", "?")
        summ = ev.get("payload_summary") or {}
        # Compact, deterministic projection. The summary values are already
        # capped by _summarize_payload — no further user-text expansion here.
        bits = ", ".join(f"{k}={v}" for k, v in summ.items() if v not in (None, "", []))
        lines.append(f"- {et}: {bits}" if bits else f"- {et}")
    return lines


async def build_memory_context(state: Any, sess: Any, ctx: dict[str, Any], *, max_tokens: int) -> str:
    """Assemble the fenced system-derived memory block (char-capped).

    `max_tokens` is the SPEC char-approx budget; the rendered block (fences
    included) never exceeds `max_tokens * 4` chars. Taste summary is kept
    first; recent turns are added newest-first until the budget is hit.
    """
    char_cap = max(0, int(max_tokens)) * 4

    taste = _taste_lines(ctx.get("user_key") or "")
    recent = await _recent_lines(ctx)

    body_lines: list[str] = []
    if taste:
        body_lines.append("Taste profile:")
        body_lines.extend(taste)
    if recent:
        body_lines.append("Recent turns (newest first):")
        body_lines.extend(recent)
    if not body_lines:
        body_lines = [_EMPTY_PLACEHOLDER]

    # Build incrementally so truncation drops OLDEST recent turns first while
    # keeping the taste summary + fences intact (REQ-AGENT-V3-MEM-CAP-001).
    def _render(lines: list[str]) -> str:
        return "\n".join([_FENCE_OPEN, *lines, _FENCE_CLOSE])

    rendered = _render(body_lines)
    if char_cap and len(rendered) > char_cap:
        # Pop trailing (oldest) recent lines one at a time until it fits.
        while len(body_lines) > 1 and len(_render(body_lines)) > char_cap:
            body_lines.pop()
        rendered = _render(body_lines)
        # Hard fallback: still over budget (huge taste line) → truncate body.
        if len(rendered) > char_cap:
            keep = max(0, char_cap - len(_FENCE_OPEN) - len(_FENCE_CLOSE) - 2)
            inner = "\n".join(body_lines)[:keep]
            rendered = "\n".join([_FENCE_OPEN, inner, _FENCE_CLOSE])
    return rendered
