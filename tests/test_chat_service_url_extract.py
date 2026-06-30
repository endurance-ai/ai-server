"""Unit tests for URL extraction in chat_service.invoke / invoke_streaming."""

from datetime import UTC, datetime

import pytest

from app.channels.schemas import ChannelMessage
from app.services.chat_service import _URL_RE


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


def test_channel_message_receives_extracted_urls() -> None:
    text = "https://pin.it/abc 이런 거 찾아줘"
    urls = _URL_RE.findall(text)
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
