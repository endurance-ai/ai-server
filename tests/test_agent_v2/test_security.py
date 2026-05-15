"""SPEC-AGENT-V2-REACT / REQ-AGENT-SEC-URL-001 + REQ-AGENT-TOOL-DISPATCH-001.

P1-4: analyze_image SSRF hard-deny (unconditional, fires even with an empty
ALLOWED_IMAGE_HOSTS allowlist).
P1-5: validate_args value-type enforcement for type-critical fields.
"""

from __future__ import annotations

import pytest

from app.agents.tool_registry import validate_args
from app.agents.tools.analyze_image import _ssrf_ok
from app.core.config import settings


@pytest.fixture
def empty_allowlist(monkeypatch):
    # Open-by-default condition: allowlist empty. Hard-deny MUST still fire.
    monkeypatch.setattr(settings, "ALLOWED_IMAGE_HOSTS", "")
    return settings


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/",  # AWS/cloud metadata
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "file:///etc/passwd",  # non-http scheme
        "javascript:alert(1)",  # non-http scheme
        "gopher://127.0.0.1/",  # non-http scheme
        "http://127.0.0.1/",  # loopback
        "http://127.0.0.1:8000/x",  # loopback w/ port
        "http://localhost/",  # loopback name
        "http://192.168.1.1/",  # RFC-1918
        "http://10.0.0.5/",  # RFC-1918
        "http://172.16.0.1/",  # RFC-1918 (172.16-31)
        "http://172.31.255.255/",  # RFC-1918 upper bound
    ],
)
def test_ssrf_rejected_even_with_empty_allowlist(empty_allowlist, url):
    ok, err = _ssrf_ok(url)
    assert ok is False, f"{url} should be blocked but passed"
    assert err


@pytest.mark.parametrize(
    "url",
    [
        "https://i.pinimg.com/originals/ab/cd/ef.jpg",
        "https://r2.cloudflarestorage.com/bucket/key.png",
    ],
)
def test_ssrf_positive_control_passes_with_empty_allowlist(empty_allowlist, url):
    ok, err = _ssrf_ok(url)
    assert ok is True, f"{url} should pass but was blocked: {err}"


def test_ssrf_172_outside_private_range_not_blocked(empty_allowlist):
    # 172.15.x and 172.32.x are public — must NOT be hard-denied.
    ok, _ = _ssrf_ok("https://172.15.0.1/x.jpg")
    assert ok is True
    ok, _ = _ssrf_ok("https://172.32.0.1/x.jpg")
    assert ok is True


# ── P1-5: validate_args value-type enforcement ─────────────────────────────


@pytest.mark.parametrize(
    "tool,args",
    [
        ("search_products", {"text_query": "x", "top_k": {"nested": 1}}),
        ("get_recent_history", {"n": "abc"}),
        ("search_products", {"text_query": "x", "min_price": "10"}),
        ("search_products", {"text_query": "x", "max_price": [1]}),
        ("search_products", {"text_query": "x", "top_k": True}),  # bool != int
        ("update_taste", {"brand_likes": "nike"}),
        ("update_taste", {"brand_dislikes": "adidas"}),
        ("update_taste", {"keyword_likes": "minimal"}),
        ("update_taste", {"keyword_dislikes": "loud"}),
        ("get_recent_history", {"event_types": "user_text"}),
        ("ask_user_clarification", {"options": "a"}),
    ],
)
def test_validate_args_rejects_wrong_value_type(tool, args):
    ok, err = validate_args(tool, args)
    assert ok is False, f"{tool} {args} should fail type check"
    assert "bad_type" in err


def test_validate_args_accepts_correct_value_types():
    ok, err = validate_args("search_products", {"text_query": "x", "top_k": 15, "min_price": 0})
    assert ok is True, err
