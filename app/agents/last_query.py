"""SPEC-AGENT-V2-REACT follow-up (260522) — last successful search query store.

`refine_search` needs the PRODUCT query from the previous search (e.g.
'grey floral lace dress women') so a refinement like "더 저렴하게 20만원 이하로"
just re-applies a price/filter delta on the SAME query. But `ctx['text_query']`
is seeded fresh from the RAW user message each turn (react_loop._build_ctx),
so on a refine turn it holds the refinement INSTRUCTION, not the product query.
Embedding that instruction returned semantically-unrelated cheap junk (live
trace 16:31: '더 저렴하게 해줘 20만원 이하로' → bags / perfume / keychain).

This in-process store keyed by chat_id holds the last successful search's final
(English, gender-pinned) text_query so `refine_search` can reuse it across the
turn boundary. Same lifecycle rationale as `pending_question` / `pending_gender`:
ephemeral conversational state, dropped on restart (harmless — a refine right
after a restart just falls back to the current message).
"""

from __future__ import annotations

from typing import Any

# chat_id -> last successful search query (English, gender-pinned).
_LAST: dict[int, str] = {}


def _coerce_chat_id(chat_id: Any) -> int | None:
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return None


def set_last_query(chat_id: Any, query: str) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    q = (query or "").strip()
    if q:
        _LAST[cid] = q[:240]


def get_last_query(chat_id: Any) -> str | None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return None
    return _LAST.get(cid)


def clear_last_query(chat_id: Any) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    _LAST.pop(cid, None)


def _reset_all_for_tests() -> None:
    _LAST.clear()
