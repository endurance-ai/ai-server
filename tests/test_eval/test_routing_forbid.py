"""routing_eval e2e 금지 도구 채점 (2026-10-18).

데이터셋 정의는 "forbid_tools=호출되면 실패"인데 e2e 채점은 첫 도구만 봤다. 저장소의
10/02 결과(C_all_sonnet)에서 pivot_brand1 이 search_products → refine_search(금지)로 갔는데도
성공으로 집계됐다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "eval" / "routing_eval.py"
_spec = importlib.util.spec_from_file_location("routing_eval", _PATH)
routing_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(routing_eval)

_CASE = {"expect_tools": ["search_products"], "forbid_tools": ["refine_search", "web_search"]}


def test_forbidden_tool_after_first_fails():
    assert routing_eval.route_ok(_CASE, "search_products")
    assert not routing_eval.forbid_ok(_CASE, ["search_products", "refine_search", "respond"])


def test_allowed_sequence_passes():
    assert routing_eval.forbid_ok(_CASE, ["search_products", "respond"])
    assert routing_eval.forbid_ok(_CASE, [])
