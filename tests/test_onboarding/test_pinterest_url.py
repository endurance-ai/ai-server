"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-002 + REQ-ONBOARD-SEC-001.

Classification matrix for `classify_pinterest_input`. Includes spoofed-host
attacks (REQ-ONBOARD-SEC-001) and PIN > BOARD > PROFILE precedence.
"""

from __future__ import annotations

import pytest

from app.channels.pinterest_url import (
    PinInputBoard,
    PinInputNone,
    PinInputPins,
    PinInputProfile,
    classify_pinterest_input,
)


def test_single_pin_url():
    r = classify_pinterest_input("https://pinterest.com/pin/111/")
    assert isinstance(r, PinInputPins)
    assert r.urls == ("https://pinterest.com/pin/111/",)
    assert r.truncated is False


def test_board_url():
    r = classify_pinterest_input("https://pinterest.com/jane/coats/")
    assert isinstance(r, PinInputBoard)
    assert "jane/coats" in r.url


def test_profile_url():
    r = classify_pinterest_input("https://pinterest.com/jane/")
    assert isinstance(r, PinInputProfile)
    assert "jane" in r.url


def test_pin_it_shortlink():
    r = classify_pinterest_input("pin.it/abc123")
    assert isinstance(r, PinInputPins)
    assert r.urls[0].startswith("https://pin.it/abc123")


def test_kr_subdomain_board():
    r = classify_pinterest_input("https://kr.pinterest.com/jane/coats/")
    assert isinstance(r, PinInputBoard)


def test_www_subdomain_pin():
    r = classify_pinterest_input("https://www.pinterest.com/pin/999/")
    assert isinstance(r, PinInputPins)


def test_pin_precedence_drops_board():
    """PIN > BOARD — board URL dropped when any pin is present."""
    text = (
        "check board https://pinterest.com/jane/coats/ "
        "and https://pinterest.com/pin/111/ https://pinterest.com/pin/222/"
    )
    r = classify_pinterest_input(text)
    assert isinstance(r, PinInputPins)
    assert len(r.urls) == 2


def test_board_precedence_beats_profile():
    text = "https://pinterest.com/jane/ https://pinterest.com/jane/coats/"
    r = classify_pinterest_input(text)
    assert isinstance(r, PinInputBoard)


def test_max_pins_cap_truncates():
    urls = " ".join(f"https://pinterest.com/pin/{i}/" for i in range(25))
    r = classify_pinterest_input(urls, max_pins=20)
    assert isinstance(r, PinInputPins)
    assert len(r.urls) == 20
    assert r.truncated is True


@pytest.mark.parametrize(
    "spoofed",
    [
        "https://pinterest.com.evil.com/pin/111/",
        "https://evil-pinterest.com/pin/111/",
        "https://pinterest.evil.com/pin/111/",
        "https://pinterest.co.kr.evil/pin/111/",
    ],
)
def test_spoofed_hosts_classify_none(spoofed):
    """REQ-ONBOARD-SEC-001 — host allowlist defeats substring spoofing."""
    r = classify_pinterest_input(spoofed)
    assert isinstance(r, PinInputNone)


def test_javascript_scheme_classify_none():
    r = classify_pinterest_input("javascript:alert(1)")
    assert isinstance(r, PinInputNone)


def test_no_pinterest_text():
    r = classify_pinterest_input("just some random text no urls here")
    assert isinstance(r, PinInputNone)


def test_empty_input():
    r = classify_pinterest_input("")
    assert isinstance(r, PinInputNone)


def test_mixed_spoofed_and_real():
    """Real pinterest URL with spoofed sibling should still recognize the real one."""
    text = "https://pinterest.com.evil.com/x/y/ https://pinterest.com/pin/777/"
    r = classify_pinterest_input(text)
    assert isinstance(r, PinInputPins)
    assert any("pin/777" in u for u in r.urls)


def test_bare_url_promoted_to_https():
    r = classify_pinterest_input("pinterest.com/pin/333/")
    assert isinstance(r, PinInputPins)
    assert r.urls[0].startswith("https://")
