"""SPEC-AGENT-V2-REACT / T-003e — `ask_user_clarification` tool wrapper.

Sends an inline-keyboard card. Callback shape `clarify:{axis}:{value}` mirrors
SPEC-CLARIFY-CARDS-001.

@MX:NOTE: [AUTO] Side effect: sends Telegram message with InlineKeyboard.
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agents.tool_registry import AskUserClarificationResult

logger = logging.getLogger(__name__)

_VALID_AXES = ("category_pick", "formality", "fit", "occasion", "subcategory_disambiguation", "generic_fallback")

# 여러 선택지를 한 문자열로 뭉쳐 보낸 경우 분리할 구분자 (쉼표/슬래시/가운뎃점/
# 세로줄/개행/한글 쉼표). 객관식이 한 버튼에 다 뭉쳐 뜨던 버그(2026-08-19) 방어.
_OPTION_SPLIT_RE = re.compile(r"\s*[,/·、|\n]\s*")

# 라벨 양끝에서 벗겨낼 감싸개 — 따옴표(직/스마트)·괄호·대괄호·중괄호·한글 괄호.
# LLM 이 '"니트"' / '(니트)' / "'가디건'" 처럼 넘겨 버튼에 노출되던 버그(2026-08-19).
_WRAP_CHARS = "\"'“”‘’「」『』()[]{}（）〔〕〈〉《》 \t"


# clarify 남발 가드 (2026-08-28): 유저가 이미 검색 가능한 신호(특정 garment /
# 브랜드 / 가격)를 줬는데도 모델이 되묻는 실패를 차단한다(실트레이스: "빈티지
# 셔츠"→회피, "Zara"→되물음, "170cm 부츠컷 청바지"→핏 되물음). 색/핏/무드만
# 있는 진짜 모호 쿼리("미니멀 무채색 옷")는 신호로 치지 않아 clarify 를 허용한다.
# 한글 garment 어휘(영문은 category_family.to_canonical_family 재사용). 오탐 위험
# 1음절/가격충돌 토큰(티·백)은 제외하고 2음절+ 명시 토큰만.
_KO_GARMENTS: frozenset[str] = frozenset(
    {
        "후드",
        "후디",
        "후드티",
        "맨투맨",
        "티셔츠",
        "반팔",
        "긴팔",
        "나시",
        "셔츠",
        "남방",
        "블라우스",
        "니트",
        "스웨터",
        "가디건",
        "자켓",
        "재킷",
        "코트",
        "패딩",
        "점퍼",
        "잠바",
        "바지",
        "팬츠",
        "청바지",
        "데님",
        "슬랙스",
        "조거",
        "반바지",
        "치마",
        "스커트",
        "원피스",
        "드레스",
        "운동화",
        "스니커즈",
        "부츠",
        "로퍼",
        "구두",
        "샌들",
        "슬리퍼",
        "가방",
        "백팩",
        "블레이저",
        "베스트",
        "조끼",
        "카고",
        "점프수트",
    }
)
# 가격 신호 — 금액/비교 표현. (req_price_max 가 이미 파싱됐으면 그걸 우선 사용.)
_PRICE_RE = re.compile(
    r"(\d[\d,]*\s*(원|만원|만|천원|k\b|won))|(under|over|이하|이상|저렴|싸게|비싸|cheaper)", re.IGNORECASE
)


def _has_search_signal(text: str, ctx: dict[str, Any]) -> str | None:
    """유저 원문에 바로 검색/refine 가능한 신호가 있으면 그 종류를 반환(없으면 None).

    garment(영/한) · brand · price 를 강한 신호로 본다. 색/핏/무드만으로는 None
    (모호 쿼리라 clarify 정당). 반환값은 로깅/에러 메시지용.
    """
    if not text or not text.strip():
        return None
    # 가격
    if ctx.get("req_price_max") or ctx.get("req_price_min") or _PRICE_RE.search(text):
        return "price"
    # 브랜드
    try:
        from app.infrastructure.repositories.brand_node_cache import scan_text_for_brand

        if scan_text_for_brand(text):
            return "brand"
    except Exception:  # noqa: BLE001 — fail-open (신호 없다고 보고 clarify 허용)
        pass
    # garment (영문: 토큰→실 family, 한글: 부분일치)
    try:
        from app.infrastructure.repositories.category_family import to_canonical_family

        for tok in re.findall(r"[a-z][a-z-]{2,}", text.lower()):
            fam = to_canonical_family(tok)
            if fam and fam != "other":
                return f"garment:{tok}"
    except Exception:  # noqa: BLE001
        pass
    if any(g in text for g in _KO_GARMENTS):
        return "garment"
    return None


def _clean_option_label(s: str) -> str:
    """옵션 라벨에서 감싼 따옴표/괄호와 양끝 잔여 인용부호를 제거한다.
    `str.strip(set)` 은 양끝의 해당 문자를 모두 벗기므로 '"(니트)"' → '니트'."""
    return s.strip().strip(_WRAP_CHARS).strip()


def _split_crammed_options(options: list[str]) -> list[str]:
    """LLM 이 객관식을 한 문자열로 뭉쳐 넘긴 경우(options=list[1]) 구분자로 분리한다.
    1-옵션 clarify 는 고를 게 없어 무의미하므로 분리는 항상 안전하다. 분리 실패
    (구분자 없음)면 원본 그대로."""
    if len(options) != 1:
        return options
    parts = [p.strip() for p in _OPTION_SPLIT_RE.split(options[0]) if p.strip()]
    return parts if len(parts) >= 2 else options


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> AskUserClarificationResult:
    axis = args.get("axis")
    # ONE COARSE CLARIFY MAX (2026-08-19): 유저가 방금 clarify 를 답했다면(큰 틀 선택)
    # 2차 좁히기를 코드로 차단 — 이중 좁히기 피로 방지, 바로 검색해 상품을 쫙 보여준다.
    # 에이전트는 이 에러를 받으면 search_products 로 진행한다.
    if ctx.get("from_clarify_answer"):
        return AskUserClarificationResult(
            ok=False,
            error="already_clarified_search_now",
            card_sent=False,
            axis=str(axis),
        )
    # clarify 남발 가드: 유저가 이미 garment/brand/price 를 줬으면 되묻지 말고
    # 바로 검색/refine. 에이전트는 이 에러를 받으면 search_products(신규) 또는
    # refine_search(직전 결과 조정: '더 저렴한', 색 변경 등)로 진행한다.
    _signal = _has_search_signal(str(ctx.get("text_query") or ""), ctx)
    if _signal:
        logger.info("🚦 [clarify] blocked — search signal=%s → search_now", _signal)
        return AskUserClarificationResult(
            ok=False,
            error=f"has_search_signal_search_now ({_signal}): user already named a garment/brand/price — "
            "call search_products (or refine_search if adjusting the previous results) now, do NOT clarify",
            card_sent=False,
            axis=str(axis),
        )
    if axis not in _VALID_AXES:
        # P0-1 (260521 V3 eval): include valid axes in the error so the LLM's
        # next iter can self-correct. The structural validator already rejects
        # this case (see tool_registry.validate_args); this is belt-and-braces
        # for direct dispatch paths that may bypass the validator.
        return AskUserClarificationResult(
            ok=False,
            error=f"invalid_axis: {axis!r} not in {list(_VALID_AXES)}",
            card_sent=False,
            axis=str(axis),
        )

    # 7/10 사고 방어: options 안의 None/빈값을 걸러내고 전부 str 로 강제.
    # (검색 실패로 후보 0개 → 카드 빌드 중 IndexError/TypeError 재발 방지)
    options = [str(o) for o in (args.get("options") or []) if o]
    # LLM 이 "니트, 가디건, 코트" 처럼 선택지를 한 문자열로 뭉쳐 넘기면 낱개로 분리.
    options = _split_crammed_options(options)
    # 감싼 따옴표/괄호 제거 후 빈값 탈락 ('"니트"' → '니트').
    options = [c for c in (_clean_option_label(o) for o in options) if c]
    prompt = (args.get("prompt") or "").strip()
    if not options or not prompt:
        return AskUserClarificationResult(ok=False, error="missing_options_or_prompt", card_sent=False, axis=axis)

    chat_id = ctx.get("chat_id")
    if chat_id is None:
        return AskUserClarificationResult(ok=False, error="missing_chat_id", card_sent=False, axis=axis)

    try:
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
        # Build a simple 1-row-per-option inline keyboard.
        buttons = [[{"text": opt[:32], "callback_data": f"clarify:{axis}:{opt}"[:64]}] for opt in options[:8]]
        if hasattr(adapter, "send_text_with_buttons"):
            await adapter.send_text_with_buttons(chat_id, prompt, buttons)
        else:
            await adapter.send_text(chat_id, prompt)
    except Exception as exc:  # noqa: BLE001
        # exc_info=True: 다음 사고 때 정확한 라인/스택을 로그에서 바로 잡기 위함.
        logger.warning("[tool.ask_user_clarification] card send failed: %r", exc, exc_info=True)
        # 7/10 사고 방어: 카드(버튼) 전송이 터져도 최소한 prompt 텍스트는 유저에게
        # 나가도록 plain-text fallback. (카드 실패 → 텍스트만 조용히 나가던 문제 방지)
        try:
            from app.graphs.nodes._adapter_ctx import get_adapter

            await get_adapter().send_text(chat_id, prompt)
        except Exception as fb_exc:  # noqa: BLE001
            logger.warning("[tool.ask_user_clarification] text fallback also failed: %r", fb_exc, exc_info=True)
            return AskUserClarificationResult(
                ok=False, error=f"send_failed:{type(exc).__name__}", card_sent=False, axis=axis
            )
        # 텍스트는 전달됐으나 카드(버튼)는 실패 — card_sent=False 로 정직하게 보고.
        return AskUserClarificationResult(
            ok=False, error=f"send_failed:{type(exc).__name__}", card_sent=False, axis=axis
        )

    return AskUserClarificationResult(ok=True, error=None, card_sent=True, axis=axis)
