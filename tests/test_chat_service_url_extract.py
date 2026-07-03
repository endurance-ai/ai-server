"""Unit tests for URL extraction in chat_service.invoke / invoke_streaming."""

from datetime import UTC, datetime

import pytest

from app.channels.schemas import ChannelMessage
from app.services.chat_service import _URL_RE, _extract_urls


@pytest.mark.parametrize(
    "text, expected_urls",
    [
        ("https://pin.it/abc 이런 거 찾아줘", ["https://pin.it/abc"]),
        ("https://www.pinterest.com/pin/123/ 비슷한 거", ["https://www.pinterest.com/pin/123/"]),
        ("https://www.instagram.com/p/abc123/ 이 옷", ["https://www.instagram.com/p/abc123/"]),
        ("청바지 찾아줘", []),
        ("", []),
        (
            "https://pin.it/abc 그리고 https://pin.it/xyz 두 개",
            ["https://pin.it/abc", "https://pin.it/xyz"],
        ),
    ],
)
def test_url_re_extracts_from_text(text: str, expected_urls: list[str]) -> None:
    assert _URL_RE.findall(text) == expected_urls


@pytest.mark.parametrize(
    "text, expected_urls",
    [
        # trailing punctuation — 실제 유저 붙여넣기 시 마침표/쉼표/괄호 등이
        # 뒤에 붙어도 순수 URL 로 정규화돼야 함. 원본 버그: pin.it/xxx. 이
        # shortcode 로 넘어가 pinterest 홈으로 리다이렉트 → 로고 og:image 를
        # 상품 이미지로 취급 → agent 가 링크 분석 실패로 응답.
        ("https://pin.it/abc.", ["https://pin.it/abc"]),
        ("이거 좀 https://pin.it/abc.", ["https://pin.it/abc"]),
        ("https://pin.it/abc, 이랑 비슷한", ["https://pin.it/abc"]),
        ("(https://pin.it/abc)", ["https://pin.it/abc"]),
        ("https://pin.it/abc?", ["https://pin.it/abc"]),
        ('"https://pin.it/abc"', ["https://pin.it/abc"]),
        (None, []),
        ("", []),
        (
            "두 개 https://pin.it/aaa) https://pin.it/bbb!",
            ["https://pin.it/aaa", "https://pin.it/bbb"],
        ),
    ],
)
def test_extract_urls_strips_trailing_punct(text: str | None, expected_urls: list[str]) -> None:
    assert _extract_urls(text) == expected_urls


def test_channel_message_receives_extracted_urls() -> None:
    text = "https://pin.it/abc 이런 거 찾아줘"
    urls = _extract_urls(text)
    msg = ChannelMessage(
        chat_id=1,
        text=text,
        urls=urls,
        received_at=datetime.now(UTC),
    )
    assert len(msg.urls) == 1
    assert "pin.it" in str(msg.urls[0])


def test_ssrf_guard_blocks_internal_url() -> None:
    urls = _URL_RE.findall("http://localhost/admin")
    msg = ChannelMessage(
        chat_id=1,
        text="http://localhost/admin",
        urls=urls,
        received_at=datetime.now(UTC),
    )
    assert msg.urls == []
