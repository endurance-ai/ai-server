"""test_diversify caplog 안정화 — chat_state 와 동일한 logger.disabled 우회.

CI 에서 `app.services.diversify_service` logger 가 사전에 disabled=True 로
세팅돼 caplog INFO 캡처가 실패하는 케이스 재현. autouse 픽스처로 매 테스트
setup 시 강제 복구.
"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_diversify_logger_disabled():
    targets = [
        logging.getLogger("app"),
        logging.getLogger("app.services"),
        logging.getLogger("app.services.diversify_service"),
    ]
    prior = [lg.disabled for lg in targets]
    for lg in targets:
        lg.disabled = False
    try:
        yield
    finally:
        for lg, was in zip(targets, prior, strict=True):
            lg.disabled = was
