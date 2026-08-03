"""Helper-function unit tests — no Postgres needed."""

from __future__ import annotations

from tests.test_implicit_feedback.conftest import FakeCandidate


def test_keywords_prefers_explicit_keywords_field():
    from app.channels.implicit_feedback import _keywords_for_product

    c = FakeCandidate(id="p1", subcategory="tee", name="x", keywords=["Wool", "  Long  ", ""])
    assert _keywords_for_product(c) == ["wool", "long"]


def test_keywords_fallback_tokenizes_subcategory_and_name():
    from app.channels.implicit_feedback import _keywords_for_product

    c = FakeCandidate(id="p1", subcategory="Denim", name="Wide Pants", keywords=[])
    out = _keywords_for_product(c)
    assert "denim" in out
    assert "wide" in out
    assert "pants" in out
    # de-duped, lowercased
    assert all(k == k.lower() for k in out)


def test_click_callback_truncates_to_53_char_suffix():
    from app.channels.implicit_feedback import _CLICK_SUFFIX_BUDGET, click_callback_for

    short_pid = "abc123"
    assert click_callback_for(short_pid) == "crit:click:abc123"

    long_pid = "x" * 60
    cb = click_callback_for(long_pid)
    assert cb.startswith("crit:click:")
    assert len(cb) <= 64
    suffix = cb[len("crit:click:") :]
    assert len(suffix) == _CLICK_SUFFIX_BUDGET
    assert long_pid.endswith(suffix)


def test_resolve_click_target_exact_match():
    from app.channels.implicit_feedback import resolve_click_target

    cs = [FakeCandidate(id="p1"), FakeCandidate(id="p2"), FakeCandidate(id="p3")]
    assert resolve_click_target("p2", cs) is cs[1]


def test_resolve_click_target_suffix_match():
    from app.channels.implicit_feedback import resolve_click_target

    long_id = "verylongproductid_abcdef"
    cs = [FakeCandidate(id=long_id), FakeCandidate(id="other")]
    # Truncated suffix should still resolve
    assert resolve_click_target(long_id[-10:], cs) is cs[0]


def test_resolve_click_target_returns_none_when_missing():
    from app.channels.implicit_feedback import resolve_click_target

    cs = [FakeCandidate(id="p1")]
    assert resolve_click_target("nonexistent", cs) is None


def test_resolve_click_target_handles_empty_suffix():
    from app.channels.implicit_feedback import resolve_click_target

    cs = [FakeCandidate(id="p1")]
    assert resolve_click_target("", cs) is None
