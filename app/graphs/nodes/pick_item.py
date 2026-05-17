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

from app.channels.lang import session_lang
from app.channels.session import SessionState, get_store
from app.channels.taste_profile import user_key_for
from app.channels.vision import derive_legacy_keywords, derive_legacy_label
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.nodes._trace import node_done, node_enter, node_skip
from app.graphs.state import WorkingState
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)

# EN scaffold (default).
PICKER_HEADER_EN = "I see {n} item{s} in this photo 👀\n\n{lines}\n\nWhich one are you after? Tap below 👇"
PICKER_LINE_EN = "{num}  {label} — {desc}"
PICK_INVALID_EN = "Tap one of the buttons above to choose an item 👆"

# KO scaffold — kiko persona, friendly 해요체.
# (KO label format is inlined in `_send_picker` since description is dropped
# in KO mode to avoid mixing languages on one line.)
PICKER_HEADER_KO = "사진에서 {n}개 아이템을 발견했어요 👀\n\n{lines}\n\n어떤 게 마음에 들어요? 아래에서 골라봐요 👇"
PICK_INVALID_KO = "위 버튼 중 하나를 골라주세요 👆"

NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]


def _ko_label(it: dict) -> str:
    """Korean-friendly label for a detected item.

    Vision LLM emits English `label` (== item.name) and `description` (== item.detail).
    For KO output we prefer `searchQueryKo` (already Korean keyword phrase) when present,
    falling back to the English label so the user still sees something meaningful.
    """
    sq_ko = (it.get("searchQueryKo") or "").strip()
    if sq_ko:
        return sq_ko
    return it.get("label") or "아이템"


async def _send_picker(adapter, chat_id: int, items: list[dict], lang: str = "en") -> None:
    n = len(items)
    lines: list[str] = []
    for i, it in enumerate(items[:4]):
        num_em = NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}."
        if lang == "ko":
            label = _ko_label(it)
            # description is English from the LLM. Skip it in KO mode rather
            # than mixing languages in one card line.
            lines.append(f"{num_em}  {label}")
        else:
            label = it.get("label") or "item"
            desc = it.get("description") or ""
            if desc:
                lines.append(PICKER_LINE_EN.format(num=num_em, label=label, desc=desc))
            else:
                lines.append(f"{num_em}  {label}")
    if lang == "ko":
        body = PICKER_HEADER_KO.format(n=n, lines="\n".join(lines))
    else:
        body = PICKER_HEADER_EN.format(n=n, s="" if n == 1 else "s", lines="\n".join(lines))
    buttons = [(NUMBER_EMOJI[i] if i < len(NUMBER_EMOJI) else f"{i + 1}", f"item:{i}") for i in range(min(n, 4))]
    if hasattr(adapter, "send_text_with_buttons"):
        await adapter.send_text_with_buttons(chat_id, body, buttons)
    else:
        await adapter.send_text(chat_id, body)


# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def _emit_pick_item_done(
    state: WorkingState,
    *,
    items: list[dict],
    picked_index: int,
    auto_picked: bool,
) -> None:
    """LOG-T14 — emit `pick_item_done` (single carousel / callback / auto-pick)."""
    try:
        emit(
            event_type="pick_item_done",
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=4,
            payload={
                "candidate_items": [
                    {"label": it.get("label", ""), "subcategory": it.get("subcategory", "")} for it in items[:10]
                ],
                "picked_index": picked_index,
                "auto_picked": auto_picked,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("[pick_item] pick_item_done emit best-effort")


@observe(name="node.pick_item", as_type="span")
async def pick_item(state: WorkingState) -> dict:
    node_enter("pick_item")
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
                    invalid_msg = "잘못된 선택이에요" if session_lang(sess) == "ko" else "Invalid choice"
                    await adapter.answer_callback_query(msg.callback_query_id, invalid_msg)
            except Exception as exc:  # REQ-AGENT-007
                logger.exception("[pick_item] answer_callback_query failed")
                breadcrumbs.append(f"pick_item_error: {type(exc).__name__}"[:120])
            breadcrumbs.append(f"pick_item: invalid idx={idx}")
            node_skip("pick_item", f"invalid idx={idx}")
            return {"log_events": breadcrumbs}

        try:
            adapter = get_adapter()
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, None)
        except Exception:
            logger.exception("[pick_item] answer_callback_query (success) failed")

        item = items[idx]
        sess.selected_item_index = idx
        # SPEC-VISION-UNIFY-001 / REQ-VISION-STATE-003 — preserve full rich
        # VisionItem when available. `vision_result` is the source of truth;
        # `detected_items[idx]` is the legacy projection.
        rich_item = None
        if sess.vision_result is not None:
            try:
                rich_items = list(sess.vision_result.items)
                if 0 <= idx < len(rich_items):
                    rich_item = rich_items[idx]
            except Exception:  # noqa: BLE001
                rich_item = None
        if rich_item is not None:
            sess.vision_selected_item_index = idx
            sess.vision_item = derive_legacy_label(rich_item) or "item"
            sess.vision_keywords = derive_legacy_keywords(rich_item)
        else:
            sess.vision_item = item.get("label") or "item"
            sess.vision_keywords = list(item.get("keywords") or [])
        # Note: original scenario sent the OPENER asking for intent. The graph
        # now flows directly to critique_apply → search_node → respond, so we
        # don't re-prompt the user here. Keep AWAITING_INTENT for symmetry with
        # the legacy state model.
        sess.state = SessionState.AWAITING_INTENT
        get_store().update(sess)
        breadcrumbs.append(f"pick_item: selected idx={idx} label={sess.vision_item}")
        _emit_pick_item_done(state, items=items, picked_index=idx, auto_picked=False)
        node_done("pick_item", picked_idx=idx, label=sess.vision_item)
        return {
            "selected_item_index": idx,
            "detected_items": items,
            "vision_selected_item": rich_item,
            "log_events": breadcrumbs,
            "turn_no": 4,
        }

    # ── Carousel-send path ─────────────────────────────────────────────────
    items = state.detected_items or sess.detected_items
    if not items:
        breadcrumbs.append("pick_item: no items to display")
        node_skip("pick_item", "no items to display")
        return {"log_events": breadcrumbs}

    try:
        adapter = get_adapter()
        await _send_picker(adapter, state.chat_id, items, lang=session_lang(sess))
    except Exception as exc:  # REQ-AGENT-007
        logger.exception("[pick_item] picker send failed")
        breadcrumbs.append(f"pick_item_error: {type(exc).__name__}: {exc}"[:200])
        node_skip("pick_item", f"picker send error {type(exc).__name__}")
        return {"log_events": breadcrumbs}

    sess.detected_items = items
    sess.state = SessionState.AWAITING_ITEM_PICK
    get_store().update(sess)
    breadcrumbs.append(f"pick_item: picker sent n={len(items)} → END")
    # Carousel sent — no specific pick yet, picked_index=-1.
    _emit_pick_item_done(state, items=items, picked_index=-1, auto_picked=False)
    node_done("pick_item", picker_sent=len(items))
    return {"detected_items": items, "log_events": breadcrumbs, "turn_no": 4}
