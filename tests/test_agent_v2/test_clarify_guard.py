"""clarify 남발 가드 — 유저가 이미 garment/brand/price 를 줬으면 되묻지 말고
바로 검색/refine 하게 강제(실트레이스: "빈티지 셔츠"→회피, "Zara"→되물음,
"170cm 부츠컷 청바지"→핏 되물음). 색/무드만 있는 모호 쿼리는 clarify 유지.
"""

from __future__ import annotations

import pytest

from app.agents.tools.ask_user_clarification import _has_search_signal, dispatch

# ── _has_search_signal ──────────────────────────────────────────────────────


def test_signal_garment_english():
    assert _has_search_signal("vintage shirt", {}) == "garment:shirt"
    assert _has_search_signal("neutral minimal jacket men", {}) == "garment:jacket"


def test_signal_garment_korean():
    assert _has_search_signal("170cm인데 롱 부츠컷 청바지", {}) == "garment"
    assert _has_search_signal("빈티지 셔츠", {}) == "garment"


def test_signal_longsleeve_korean():
    # 실트레이스 2026-09-05: "롱슬리브 찾아줘"인데 clarify 2번 남발. 롱슬리브도 garment.
    assert _has_search_signal("약간 빈티지한 무든데 걸리시한 롱슬리브 찾아줘", {}) == "garment"
    assert _has_search_signal("뭔소리야 롱슬리브 찾으라고", {}) == "garment"


def test_signal_price_takes_priority():
    # 가격 신호가 먼저 잡힌다("더 저렴한" / "10만원 이하").
    assert _has_search_signal("이 후디보다 더 저렴한 후디", {}) == "price"
    assert _has_search_signal("10만원 이하 후드", {}) == "price"
    assert _has_search_signal("cheaper hoodie", {}) == "price"


def test_signal_price_from_ctx():
    assert _has_search_signal("아무거나", {"req_price_max": 100000}) == "price"


def test_vague_mood_has_no_signal():
    # 케이스 B — 특정 garment 없이 무드/색만 → clarify 정당(신호 없음).
    assert _has_search_signal("미니멀하면서 스트릿한 무채색 옷", {}) is None
    assert _has_search_signal("recommend something nice", {}) is None
    assert _has_search_signal("", {}) is None
    assert _has_search_signal("옷 추천해줘", {}) is None


# ── dispatch 가드 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_blocks_clarify_when_garment_present():
    # garment 신호 → 카드 전송 없이 has_search_signal 에러 반환(side-effect 전 조기 종료).
    res = await dispatch(
        {"axis": "fit", "options": ["slim", "loose"]},
        {"text_query": "롱 부츠컷 청바지"},
    )
    assert res["ok"] is False
    assert res["card_sent"] is False
    assert "has_search_signal" in str(res["error"])


@pytest.mark.asyncio
async def test_dispatch_blocks_when_garment_in_raw_user_msg_only():
    # 실트레이스: 검색 전 clarify 턴이라 text_query 는 비었지만 원문(user_msg)에
    # garment("롱슬리브")가 있으면 차단돼야 한다(에이전트 영어번역만 보면 놓침).
    res = await dispatch(
        {"axis": "subcategory_disambiguation", "options": ["셔츠", "니트"]},
        {"text_query": "", "user_msg": "뭔소리야 롱슬리브 찾으라고"},
    )
    assert res["ok"] is False
    assert res["card_sent"] is False
    assert "has_search_signal" in str(res["error"])


@pytest.mark.asyncio
async def test_dispatch_allows_clarify_when_vague():
    # 모호 쿼리는 신호 가드를 통과한다(이후 axis 검증 단계로). invalid axis 로
    # 신호 가드가 아닌 다른 에러가 나오는지 확인 → 신호 가드는 통과했음을 방증.
    res = await dispatch(
        {"axis": "not_a_real_axis", "options": ["a", "b"]},
        {"text_query": "미니멀한 옷"},
    )
    assert res["ok"] is False
    assert "invalid_axis" in str(res["error"])
