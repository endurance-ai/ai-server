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
# @MX:WARN: [AUTO] process-local + unbounded; written on EVERY successful search.
# @MX:REASON: single-worker assumption (review P1-D 260522). Multi-worker: a refine
#   landing on a different worker misses → falls back to ctx['text_query'] (quality
#   degrade, not data leak). At scale move to Redis (chat_state already exists) and
#   add eviction/TTL. Same pattern as pending_question.py.
_LAST: dict[int, str] = {}

# chat_id -> 직전 성공 검색의 브랜드 필터. refine("다른 색상으로")가 브랜드를
# 유지하도록 텍스트와 별도로 보관한다(브랜드는 text_query 가 아니라 구조화 필터라
# last_query 텍스트엔 안 실림). 2026-08-19: refine 이 브랜드를 잃고 다른 브랜드
# 상품을 뽑던 버그 대응. 수명/멀티워커 특성은 _LAST 와 동일.
_LAST_BRAND: dict[int, list[str]] = {}


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
    _LAST_BRAND.pop(cid, None)


def set_last_brand(chat_id: Any, brands: list[str] | None) -> None:
    """직전 검색의 브랜드 필터 보관. 빈/None 이면 이전 값을 지워 stale 브랜드가
    다음 refine 에 새지 않게 한다 (브랜드 없는 검색 후 refine 은 브랜드 무)."""
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    clean = [str(b).strip() for b in (brands or []) if str(b).strip()]
    if clean:
        _LAST_BRAND[cid] = clean[:8]
    else:
        _LAST_BRAND.pop(cid, None)


def get_last_brand(chat_id: Any) -> list[str] | None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return None
    return _LAST_BRAND.get(cid)


# chat_id -> 대화에서 감지된 sticky 브랜드 컨텍스트. `_LAST_BRAND`(직전 성공
# 검색의 브랜드, 브랜드-없는 검색마다 지워짐)와 달리, 이건 사용자가 브랜드를
# 언급하면 세팅되고 clarify 연속 턴('글로니 → (상의) → (바지)')에서 유지된다.
# 새 화제(브랜드 없는 fresh 텍스트 쿼리)에서 지워진다. 수명/멀티워커 특성은
# _LAST 와 동일(process-local, 재시작 시 소멸 — 무해).
_PINNED_BRAND: dict[int, list[str]] = {}


def set_pinned_brand(chat_id: Any, brands: list[str] | None) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    clean = [str(b).strip() for b in (brands or []) if str(b).strip()]
    if clean:
        _PINNED_BRAND[cid] = clean[:8]
    else:
        _PINNED_BRAND.pop(cid, None)


def get_pinned_brand(chat_id: Any) -> list[str] | None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return None
    return _PINNED_BRAND.get(cid)


def clear_pinned_brand(chat_id: Any) -> None:
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    _PINNED_BRAND.pop(cid, None)


def _reset_all_for_tests() -> None:
    _LAST.clear()
    _LAST_BRAND.clear()
    _PINNED_BRAND.clear()
