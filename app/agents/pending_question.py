"""Pending-question carry-across-turn state (2026-05-20).

When the `respond` tool sends a clarifying question (text ending with ?), we
remember the question text + the original user intent so the NEXT turn can
splice a short affirmative reply ("어"/"yes"/"ok") back into the original
intent — instead of letting the agent treat "어" as a fragment fresh query
and re-asking.

Storage is in-process module state keyed by chat_id. NOT persisted across
restarts on purpose: pending state without a fresh user response context is
not useful, and restarts cleanly drop it. The PG-backed Session store
re-creates dataclasses on every read, so persisting on the dataclass would
silently disappear on PG fetches; keeping the pending dict separate avoids
that footgun.

API:
  set_pending(chat_id, question_text, original_intent)
  pop_pending(chat_id) -> (question_text, original_intent) | (None, None)
  clear_pending(chat_id)
  peek_pending(chat_id) -> (question_text, original_intent) | (None, None)

All operations are best-effort and tolerate invalid chat_id (None/non-int)
by no-op'ing — every caller is best-effort UX-only.
"""

from __future__ import annotations

from typing import Any

# chat_id -> (question_text, original_intent). Bounded to a few hundred KB
# in the worst case (active chats * ~500 chars), well within memory budget.
_PENDING: dict[int, tuple[str, str | None]] = {}


def _coerce_chat_id(chat_id: Any) -> int | None:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None


def set_pending(chat_id: Any, question_text: str, original_intent: str | None) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None or not question_text:
        return
    q = str(question_text).strip()[:240]
    intent = str(original_intent).strip()[:240] if original_intent else None
    if not q:
        return
    _PENDING[cid] = (q, intent)


def peek_pending(chat_id: Any) -> tuple[str | None, str | None]:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return (None, None)
    entry = _PENDING.get(cid)
    if entry is None:
        return (None, None)
    return entry


def pop_pending(chat_id: Any) -> tuple[str | None, str | None]:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return (None, None)
    entry = _PENDING.pop(cid, None)
    if entry is None:
        return (None, None)
    return entry


def clear_pending(chat_id: Any) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    _PENDING.pop(cid, None)


def _reset_all_for_tests() -> None:
    _PENDING.clear()
