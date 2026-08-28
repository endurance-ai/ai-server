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

from collections import deque
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


# chat_id -> 최근 성공 search_products 링버퍼 (newest last). `_LAST` 는 단일
# 슬롯이라 매 검색마다 덮여, "다시 닝닝 스타일" 처럼 예전 화제를 되부르는 발화가
# 앵커를 잃던 버그(2026-08-24 실트레이스: 닝닝→스킴스 후 "다시 닝닝" → 완전 다른
# 룩) 대응. 각 원소 = {"label": 사용자 원문 발췌, "q": 해석된 영어 쿼리, "brand": [...]}.
# _memory_context 가 digest 로 노출 → 모델이 "다시/그거/again" 발화에 재사용.
# 수명/멀티워커 특성은 _LAST 와 동일(process-local, 재시작 시 소멸 — 무해).
_RECENT_MAX = 5
_RECENT: dict[int, deque[dict[str, Any]]] = {}


def push_recent_search(chat_id: Any, query: str, *, brand: list[str] | None = None, label: str = "") -> None:
    """성공 검색을 링버퍼에 기록. 같은 턴에서 두 번(원쿼리→brand-pivot 병합) 불려도
    label 이 같으면 마지막 엔트리를 갱신(중복 방지)."""
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return
    q = (query or "").strip()
    if not q:
        return
    lbl = (label or "").strip()[:80]
    entry = {"label": lbl, "q": q[:240], "brand": [str(b) for b in (brand or []) if str(b).strip()][:8] or None}
    dq = _RECENT.setdefault(cid, deque(maxlen=_RECENT_MAX))
    if dq and dq[-1].get("label") == lbl:
        dq[-1] = entry  # 같은 턴 재-persist → 갱신
        return
    dq.append(entry)


def get_recent_searches(chat_id: Any) -> list[dict[str, Any]]:
    """최근 검색을 newest-first 로 반환."""
    cid = _coerce_chat_id(chat_id)
    if cid is None:
        return []
    dq = _RECENT.get(cid)
    return list(reversed(dq)) if dq else []


def _reset_all_for_tests() -> None:
    _LAST.clear()
    _LAST_BRAND.clear()
    _PINNED_BRAND.clear()
    _RECENT.clear()
