"""SPEC-AGENT-001 / REQ-AGENT-004 (node 4/10) — pick_item.

Two paths:
1. Multi-item vision result, no current selection → render the picker carousel
   via the channel adapter, leave `selected_item_index=None` so routing emits
   `__end__` (REQ-AGENT-010 — picker-sent-only path bypasses respond).
2. Callback `item:N` → resolve N against `detected_items`, write
   `selected_item_index`, persist the selection in SessionStore (vision_item /
   vision_keywords / state=AWAITING_INTENT) so subsequent webhooks can refer
   to it. Routing then sends us to `critique_apply`.

Side effects (adapter calls, session writes) are deliberate — they preserve
the existing scenario.py behavior (REQ-COMPAT-*).
"""

from __future__ import annotations

import logging

from app.channels.session import SessionState, get_store
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.state import WorkingState

logger = logging.getLogger(__name__)

PICKER_HEADER = "I see {n} item{s} in this photo 👀\n\n{lines}\n\nWhich one are you after? Tap below 👇"
PICKER_LINE = "{num}  {label} — {desc}"
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
PICK_INVALID = "Tap one of the buttons above to choose an item 👆"


async def _send_picker(adapter, chat_id: int, items: list[dict]) -> None:
    n = len(items)
    lines: list[str] = []
    for i, it in enumerate(items[:4]):
        num_em = NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}."
        label = it.get("label") or "item"
        desc = it.get("description") or ""
        if desc:
            lines.append(PICKER_LINE.format(num=num_em, label=label, desc=desc))
        else:
            lines.append(f"{num_em}  {label}")
    body = PICKER_HEADER.format(n=n, s="" if n == 1 else "s", lines="\n".join(lines))
    buttons = [(NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}", f"item:{i}") for i in range(min(n, 4))]
    if hasattr(adapter, "send_text_with_buttons"):
        await adapter.send_text_with_buttons(chat_id, body, buttons)
    else:
        await adapter.send_text(chat_id, body)


async def pick_item(state: WorkingState) -> dict:
    msg = state.message
    sess = get_store().get_or_create(state.chat_id)
    breadcrumbs: list[str] = []

    # ── Callback path: item:N ──────────────────────────────────────────────
    if msg.callback_data and msg.callback_data.startswith("item:"):
        try:
            idx = int(msg.callback_data.split(":", 1)[1])
        except (ValueError, IndexError):
            idx = -1

        items = sess.detected_items or state.detected_items
        if not (0 <= idx < len(items)):
            try:
                adapter = get_adapter()
                if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                    await adapter.answer_callback_query(msg.callback_query_id, "Invalid choice")
            except Exception as exc:  # REQ-AGENT-007
                logger.exception("[pick_item] answer_callback_query failed")
                breadcrumbs.append(f"pick_item_error: {type(exc).__name__}"[:120])
            breadcrumbs.append(f"pick_item: invalid idx={idx}")
            return {"log_events": breadcrumbs}

        try:
            adapter = get_adapter()
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:
            logger.exception("[pick_item] answer_callback_query (success) failed")

        item = items[idx]
        sess.selected_item_index = idx
        sess.vision_item = item.get("label") or "item"
        sess.vision_keywords = list(item.get("keywords") or [])
        # Note: original scenario sent the OPENER asking for intent. The graph
        # now flows directly to critique_apply → search_node → respond, so we
        # don't re-prompt the user here. Keep AWAITING_INTENT for symmetry with
        # the legacy state model.
        sess.state = SessionState.AWAITING_INTENT
        get_store().update(sess)
        breadcrumbs.append(f"pick_item: selected idx={idx} label={sess.vision_item}")
        return {
            "selected_item_index": idx,
            "detected_items": items,
            "log_events": breadcrumbs,
        }

    # ── Carousel-send path ─────────────────────────────────────────────────
    items = state.detected_items or sess.detected_items
    if not items:
        breadcrumbs.append("pick_item: no items to display")
        return {"log_events": breadcrumbs}

    try:
        adapter = get_adapter()
        await _send_picker(adapter, state.chat_id, items)
    except Exception as exc:  # REQ-AGENT-007
        logger.exception("[pick_item] picker send failed")
        breadcrumbs.append(f"pick_item_error: {type(exc).__name__}: {exc}"[:200])
        return {"log_events": breadcrumbs}

    sess.detected_items = items
    sess.state = SessionState.AWAITING_ITEM_PICK
    get_store().update(sess)
    breadcrumbs.append(f"pick_item: picker sent n={len(items)} → END")
    return {"detected_items": items, "log_events": breadcrumbs}
