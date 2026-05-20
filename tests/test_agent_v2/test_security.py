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


# ── P1-2/4: _is_blocked_host IP canonicalization (numeric/hex/octal/IPv6) ──


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",  # decimal-encoded 127.0.0.1
        "http://0x7f000001/",  # hex-encoded 127.0.0.1
        "http://017700000001/",  # octal-encoded 127.0.0.1
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6 loopback
        "http://[::ffff:10.0.0.1]/",  # IPv4-mapped IPv6 RFC-1918
        "http://3232235521/",  # decimal-encoded 192.168.0.1
    ],
)
def test_ssrf_blocks_canonicalized_loopback(empty_allowlist, url):
    ok, err = _ssrf_ok(url)
    assert ok is False, f"{url} canonicalizes to a blocked IP but passed"
    assert err


def test_ssrf_canonicalization_public_ip_not_blocked(empty_allowlist):
    # 134744072 == 8.8.8.8 (public) — canonicalization must not over-block.
    ok, _ = _ssrf_ok("https://134744072/x.jpg")
    assert ok is True


# ── P1-5: validate_args value-type enforcement ─────────────────────────────


# NOTE (2026-05-20): search_products.top_k REMOVED from LLM schema — int-only
# validation tests now use get_recent_history.n (the surviving int field).
# Coercion logic is identical (both go through the `("top_k", "n")` loop).
@pytest.mark.parametrize(
    "tool,args",
    [
        # Genuinely unconvertible: dict for int field, list for number field, etc.
        ("get_recent_history", {"n": {"nested": 1}}),
        ("get_recent_history", {"n": "abc"}),  # non-digit str
        ("search_products", {"text_query": "x", "max_price": [1]}),
        ("get_recent_history", {"n": True}),  # bool != int (ambiguous intent)
    ],
)
def test_validate_args_rejects_wrong_value_type(tool, args):
    ok, err = validate_args(tool, args)
    assert ok is False, f"{tool} {args} should fail type check"
    assert "bad_type" in err


def test_validate_args_accepts_correct_value_types():
    ok, err = validate_args("search_products", {"text_query": "x", "min_price": 0})
    assert ok is True, err
    ok, err = validate_args("get_recent_history", {"n": 15})
    assert ok is True, err


# ── AUTO-CAST (2026-05-20): safe coercions when LLM sends type-mismatched JSON ─
@pytest.mark.parametrize(
    "tool,args,expected",
    [
        # int fields: str of digits → int
        ("get_recent_history", {"n": "20"}, {"n": 20}),
        # int fields: integer-valued float → int
        ("get_recent_history", {"n": 20.0}, {"n": 20}),
        # number fields: str → float
        ("search_products", {"text_query": "x", "min_price": "10"}, {"min_price": 10.0}),
        ("search_products", {"text_query": "x", "max_price": "59.99"}, {"max_price": 59.99}),
        # list fields: lone non-empty str → [str] (NEVER list(str) per anti-explosion guard)
        ("update_taste", {"brand_likes": "nike"}, {"brand_likes": ["nike"]}),
        ("update_taste", {"brand_dislikes": "adidas"}, {"brand_dislikes": ["adidas"]}),
        ("update_taste", {"keyword_likes": "minimal"}, {"keyword_likes": ["minimal"]}),
        ("update_taste", {"keyword_dislikes": "loud"}, {"keyword_dislikes": ["loud"]}),
        ("get_recent_history", {"event_types": "user_text"}, {"event_types": ["user_text"]}),
        ("ask_user_clarification", {"axis": "fit", "options": "a"}, {"options": ["a"]}),
        # list fields: empty str → [] (drops the field as no-op)
        ("update_taste", {"brand_likes": ""}, {"brand_likes": []}),
    ],
)
def test_validate_args_auto_casts_safe_mismatches(tool, args, expected):
    """LLM (Haiku 4.5) occasionally sends `n="20"` / `boost_keywords="tee"`
    instead of int / list. Strict rejection burned ReAct iters. Auto-cast keeps
    the loop on the happy path. Critical: never `list(str)` — must wrap as [str]
    to avoid `["t","-","s","h","i","r","t"]` per-char explosion."""
    ok, err = validate_args(tool, args)
    assert ok is True, f"{tool} {args} should auto-cast (err={err})"
    for key, want in expected.items():
        assert args[key] == want, f"{tool}.{key}: expected {want!r}, got {args[key]!r}"


@pytest.mark.parametrize(
    "tool,args",
    [
        # Anti-explosion guard: list field must reject non-str/non-list (dict, int, ...)
        ("update_taste", {"brand_likes": {"x": 1}}),
        ("update_taste", {"brand_likes": 42}),
        # int field: str that isn't digit-only
        ("get_recent_history", {"n": "fifteen"}),
        # int field: float with fractional part (not safely castable to int)
        ("get_recent_history", {"n": 15.7}),
        # number field: garbage str
        ("search_products", {"text_query": "x", "min_price": "abc"}),
    ],
)
def test_validate_args_auto_cast_still_rejects_unconvertible(tool, args):
    ok, err = validate_args(tool, args)
    assert ok is False, f"{tool} {args} should reject (unconvertible)"
    assert "bad_type" in err


def test_search_products_top_k_now_unknown_key():
    """top_k removed from LLM-facing schema (2026-05-20). If LLM somehow still
    sends it, validate_args rejects as unknown_keys (not bad_type). dispatch
    default (15) is the only correct value going forward."""
    ok, err = validate_args("search_products", {"text_query": "x", "top_k": 15})
    assert ok is False
    assert "unknown_keys" in err
    assert "top_k" in err
