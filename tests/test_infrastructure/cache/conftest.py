"""SPEC-CHAT-STATE-REDIS-001 — test_chat_state caplog 안정화.

다른 test 가 logging.config / dictConfig 류 호출을 거치면서 `app` (및 그
자손인 `app.infrastructure.cache.chat_state`) logger 의 `disabled=True` 가
세팅돼 후속 chat_state 테스트의 DEBUG 로그가 caplog 에 안 잡히는 CI 이슈
재현됨 (로컬 전체 run + CI 양쪽). autouse 픽스처로 매 테스트 시작 시 그
disabled 플래그를 강제로 False 로 되돌려 fail-open DEBUG 어설션을 안정화.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_chat_state_logger_disabled():
    """매 테스트 setup 에서 chat_state + 부모 'app' logger 의 disabled 를
    False 로 강제 — 다른 테스트가 dictConfig 등으로 끈 상태에서 들어와도
    caplog 가 DEBUG 를 정상 캡처하도록.
    """
    targets = [
        logging.getLogger("app"),
        logging.getLogger("app.infrastructure"),
        logging.getLogger("app.infrastructure.cache"),
        logging.getLogger("app.infrastructure.cache.chat_state"),
    ]
    prior = [lg.disabled for lg in targets]
    for lg in targets:
        lg.disabled = False
    try:
        yield
    finally:
        for lg, was in zip(targets, prior, strict=True):
            lg.disabled = was
