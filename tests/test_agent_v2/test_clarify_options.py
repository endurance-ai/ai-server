"""ask_user_clarification 옵션 분리 + 이중 clarify 차단 (2026-08-19).

버그: LLM 이 객관식을 한 문자열로 뭉쳐 넘겨(options=list[1]) 모바일에서 선택지가
한 버튼에 다 뭉쳐 떴다. 또한 상의→셔츠/니트 식 이중 좁히기가 피로를 유발했다.
- `_split_crammed_options`: 뭉친 옵션을 구분자로 낱개 분리.
- dispatch 가드: 유저가 clarify 를 답한 턴이면 2차 clarify 를 차단(검색 유도).
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.tools.ask_user_clarification import (
    _clean_option_label,
    _split_crammed_options,
    dispatch,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"니트"', "니트"),
        ("(가디건)", "가디건"),
        ("'코트'", "코트"),
        ("“셔츠”", "셔츠"),  # smart quotes
        ("[티셔츠]", "티셔츠"),
        ("  니트  ", "니트"),
        ("니트", "니트"),
    ],
)
def test_clean_option_label_strips_wrappers(raw: str, expected: str) -> None:
    assert _clean_option_label(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["니트, 가디건, 코트, 셔츠"], ["니트", "가디건", "코트", "셔츠"]),
        (["슬림 / 레귤러 / 오버핏"], ["슬림", "레귤러", "오버핏"]),
        (["니트·가디건·코트"], ["니트", "가디건", "코트"]),
        (["daily | work | party"], ["daily", "work", "party"]),
    ],
)
def test_split_crammed_single_option(raw: list[str], expected: list[str]) -> None:
    assert _split_crammed_options(raw) == expected


def test_already_separate_options_unchanged() -> None:
    opts = ["니트", "가디건"]
    assert _split_crammed_options(opts) == opts


def test_single_option_no_separator_unchanged() -> None:
    # 구분자 없는 단일 문자열은 분리 불가 → 그대로 (degenerate clarify, 상위서 처리).
    assert _split_crammed_options(["오버사이즈 후드티"]) == ["오버사이즈 후드티"]


def test_empty_unchanged() -> None:
    assert _split_crammed_options([]) == []


def test_second_clarify_blocked_after_clarify_answer() -> None:
    # 유저가 방금 clarify 를 답한 턴(from_clarify_answer)이면 2차 clarify 는 차단.
    ctx = {"from_clarify_answer": True, "chat_id": 1}
    args = {"axis": "subcategory_disambiguation", "prompt": "상의 종류?", "options": ["셔츠", "니트"]}
    res = asyncio.run(dispatch(args, ctx))
    assert res["ok"] is False
    assert res["error"] == "already_clarified_search_now"
    assert res["card_sent"] is False
