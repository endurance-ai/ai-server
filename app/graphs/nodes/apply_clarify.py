"""SPEC-CLARIFY-CARDS-001 / REQ-CLARIFY-CALLBACK-002 — apply_clarify 노드.

`clarify:<axis>:<value>` 콜백을 받아 ClarifyDelta 로 풀고, 검색 입력 보강을
세션과 WorkingState 양쪽에 기록한 뒤 search_node 로 진입한다.

쓰는 곳:
  - `session.boost_keywords` (sticky — REQ-CLARIFY-VALUE-MAPPING-001 / R6).
    self-critique fast-path 가 critique_delta 를 갈아치워도 살아남도록 세션
    에 둔다. search.py `_build_request` 가 다음 호출에서 합산.
  - `vision_selected_item.subcategory` 강제 적용을 위해 session 의 derived
    fields(vision_item, vision_keywords)에 영향을 주지 않고, WorkingState
    의 `clarify_delta` 에 표현을 두어 search 가 critique_delta 의
    boost_keywords 슬롯에 합치도록 한다(다음 retry 때도 보존).
  - skip 콜백 → delta 없이 그대로 search 진입(REQ-CLARIFY-CARD-002).

stale 콜백(파싱 실패) → toast + respond.
"""

from __future__ import annotations

import logging

from langchain_core.messages import SystemMessage

from app.channels.clarify import ClarifyDelta, parse_callback
from app.channels.critique import CritiqueDelta
from app.channels.session import SessionState, get_store
from app.graphs.nodes._adapter_ctx import get_adapter
from app.graphs.state import WorkingState
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


_TOAST_TEMPLATES_KO: dict[str, str] = {
    "formality": "{label} 분위기로 찾아볼게요",
    "fit": "{label} 핏으로 찾아볼게요",
    "occasion": "{label} 자리에 어울리는 걸로 골라볼게요",
    "category_pick": "{label} 위주로 찾아볼게요",
    "subcategory_disambiguation": "{label} 위주로 찾아볼게요",
    "generic_fallback": "{label} 위주로 찾아볼게요",
}
_TOAST_SKIP_KO = "그대로 검색해 볼게요"


def _toast_for(delta: ClarifyDelta) -> str:
    if delta.is_skip:
        return _TOAST_SKIP_KO
    template = _TOAST_TEMPLATES_KO.get(delta.axis.value, "{label} 으로 찾아볼게요")
    # value 는 영문 enum — 사용자 친화 라벨이 필요하면 clarify_values 로 lookup.
    from app.channels.clarify_values import get_option

    opt = get_option(delta.axis.value, delta.value)
    label = opt.label_ko if opt else delta.value
    return template.format(label=label)


def _merge_session_boost(sess, new_keywords: list[str]) -> list[str]:
    """sess.boost_keywords 에 새 키워드 합산(중복 제거, 소문자)."""
    existing = list(getattr(sess, "boost_keywords", None) or [])
    for k in new_keywords:
        kk = (k or "").strip().lower()
        if kk and kk not in {x.strip().lower() for x in existing}:
            existing.append(kk)
    return existing


@observe(name="node.apply_clarify", as_type="span")
async def apply_clarify(state: WorkingState) -> dict:
    msg = state.message
    breadcrumbs: list[str] = []

    cb = msg.callback_data or ""
    delta = parse_callback(cb, state.vision_result) if cb else None

    # ── stale / 잘못된 콜백 ──────────────────────────────────────────────
    if delta is None:
        try:
            adapter = get_adapter()
            if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
                await adapter.answer_callback_query(msg.callback_query_id, "Out of date — try a fresh search")
        except Exception:
            logger.debug("[apply_clarify] answer_callback_query best-effort")
        breadcrumbs.append("apply_clarify: stale callback")
        return {"log_events": breadcrumbs}

    # ── toast (best-effort) ──────────────────────────────────────────────
    try:
        adapter = get_adapter()
        if msg.callback_query_id and hasattr(adapter, "answer_callback_query"):
            await adapter.answer_callback_query(msg.callback_query_id, _toast_for(delta))
    except Exception:
        logger.debug("[apply_clarify] toast best-effort")

    # ── skip — delta 없이 검색으로 ─────────────────────────────────────────
    sess = get_store().get_or_create(state.chat_id)
    sess.state = SessionState.SEARCHING
    sess.clarify_axis = delta.axis.value
    sess.clarify_value = delta.value

    if delta.is_skip:
        # boost_keywords 는 그대로 유지(이전 clarify 가 있었을 수 있음).
        get_store().update(sess)
        logger.info("[CLARIFY-APPLY] axis=%s value=skip", delta.axis.value)
        breadcrumbs.append(f"apply_clarify: skip axis={delta.axis.value}")
        # critique_delta 는 None — 검색은 weak-vision 그대로 수행.
        # search_node 는 critique_delta=None 도 정상 처리.
        return {
            "clarify_axis": delta.axis,
            "clarify_value": delta.value,
            "clarify_delta": delta,
            "log_events": breadcrumbs,
        }

    # ── value 적용 ─────────────────────────────────────────────────────────
    # 1) sticky boost_keywords 는 세션에 누적(R6 — fast-path 생존).
    sess.boost_keywords = _merge_session_boost(sess, delta.keywords_to_boost)

    # 2) subcategory_override — legacy + rich vision 컨텍스트 모두 보강.
    if delta.subcategory_override:
        # legacy 시그널은 vision_result 유무와 무관하게 항상 갱신.
        sess.vision_item = delta.subcategory_override
        if sess.vision_result is not None:
            try:
                rich_items = list(sess.vision_result.items)
                idx = sess.vision_selected_item_index
                if idx is None and len(rich_items) == 1:
                    idx = 0
                if idx is not None and 0 <= idx < len(rich_items):
                    # Pydantic v2 — model_copy 로 갱신.
                    rich_items[idx] = rich_items[idx].model_copy(update={"subcategory": delta.subcategory_override})
                    sess.vision_result = sess.vision_result.model_copy(update={"items": rich_items})
            except Exception:  # noqa: BLE001
                logger.debug("[apply_clarify] subcategory override best-effort")

    # 3) searchQueryKo augment — 세션의 user_intent 끝에 덧붙여 search 가 picks.
    if delta.searchQueryKo_augment:
        base = (sess.user_intent or "").strip()
        addition = delta.searchQueryKo_augment.strip()
        if not base:
            sess.user_intent = addition
        elif addition.lower() not in base.lower():
            sess.user_intent = f"{base} {addition}"

    get_store().update(sess)

    # 4) 이번 검색에 적용할 critique_delta 합성 — boost_keywords 동시 주입(첫
    #    번째 검색에 즉시 효과). retry 시에는 self-critique 가 critique_delta 를
    #    갈아치우지만, sess.boost_keywords 가 sticky 하므로 search._build_request
    #    가 다음 호출에서도 합산해 사용한다(아래 search 패치 참조).
    legacy_delta = CritiqueDelta(
        op="free_text",
        boost_keywords=list(delta.keywords_to_boost),
        extra_intent=delta.searchQueryKo_augment,
    )

    summary = (
        f"axis={delta.axis.value} value={delta.value} "
        f"boost={','.join(delta.keywords_to_boost) or '-'} "
        f"sub={delta.subcategory_override or '-'}"
    )
    logger.info(
        "[CLARIFY-APPLY] axis=%s value=%s subcategory_override=%s boost_keywords=%s",
        delta.axis.value,
        delta.value,
        delta.subcategory_override or "null",
        ",".join(delta.keywords_to_boost) or "null",
    )
    breadcrumbs.append(f"apply_clarify: {summary[:160]}")

    return {
        "clarify_axis": delta.axis,
        "clarify_value": delta.value,
        "clarify_delta": delta,
        "critique_delta": legacy_delta,
        "messages": [SystemMessage(content=f"clarify: {summary}")],
        "log_events": breadcrumbs,
    }
