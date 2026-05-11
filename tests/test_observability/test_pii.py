"""REQ-OBS-PII-001 — `hash_id` is the sole sha256-prefix-16 helper."""

from __future__ import annotations

import hashlib

from app.observability.pii import hash_id


def test_hash_id_deterministic_int() -> None:
    a = hash_id(12345)
    b = hash_id(12345)
    assert a == b
    assert a == hashlib.sha256(b"12345").hexdigest()[:16]
    assert len(a) == 16


def test_hash_id_str_matches_int() -> None:
    assert hash_id("12345") == hash_id(12345)


def test_hash_id_none_returns_none() -> None:
    assert hash_id(None) is None


def test_hash_id_raw_value_not_leaked() -> None:
    out = hash_id(999999999)
    assert "999999999" not in out
    assert len(out) == 16


def test_hash_id_is_hex() -> None:
    out = hash_id("abc")
    int(out, 16)  # raises on non-hex
