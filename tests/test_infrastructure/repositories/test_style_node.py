"""Unit tests for the style-node code→id resolver cache.

SPEC-SEARCH-V6-STYLE-WIRING. A static A..U fallback map, replaced in-place by
`warm_cache()` when the DB is reachable. Tests cover the pure `code_to_id`
resolver, snapshot/list/digest accessors, and the warm fail-open + success
paths via a mocked `run_in_pool_loop`.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.infrastructure.repositories import style_node as sn
from app.providers import db_pool


@pytest.fixture
def isolate_cache(monkeypatch):
    """Fresh fallback-seeded cache + empty nodes/digest per test."""
    monkeypatch.setattr(sn, "_cache", dict(sn._FALLBACK))
    monkeypatch.setattr(sn, "_nodes", [])
    monkeypatch.setattr(sn, "_digest", [])
    monkeypatch.setattr(sn, "_warmed", False)


# ── code_to_id (pure) ───────────────────────────────────────────────────────


def test_code_to_id_fallback_letters(isolate_cache):
    assert sn.code_to_id("A") == 1
    assert sn.code_to_id("U") == 21


def test_code_to_id_case_and_whitespace_insensitive(isolate_cache):
    assert sn.code_to_id("  a ") == 1
    assert sn.code_to_id("b") == 2


@pytest.mark.parametrize("bad", [None, "", "AA", "1", "$", "ZZ"])
def test_code_to_id_invalid_returns_none(bad, isolate_cache):
    # 'Z' alone is a valid single letter but absent from the 21-letter map.
    assert sn.code_to_id(bad) is None


def test_code_to_id_valid_letter_outside_map_is_none(isolate_cache):
    # V..Z are valid ascii letters but not in the A..U fallback.
    assert sn.code_to_id("Z") is None


def test_snapshot_returns_copy(isolate_cache):
    snap = sn.snapshot()
    assert snap["A"] == 1
    snap["A"] = 999  # mutating the copy must not affect the cache
    assert sn.code_to_id("A") == 1


# ── warm fail-open paths ────────────────────────────────────────────────────


async def test_warm_empty_dsn_keeps_fallback(isolate_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "")
    await sn.warm_cache()
    assert sn.is_warmed() is True
    assert sn.code_to_id("A") == 1  # fallback intact


async def test_warm_pool_none_keeps_fallback(isolate_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", None)
    await sn.warm_cache()
    assert sn.is_warmed() is True
    assert sn.code_to_id("A") == 1


async def test_warm_empty_rows_keeps_fallback(isolate_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", object())

    def _fake_run(coro):
        coro.close()
        return []

    monkeypatch.setattr(db_pool, "run_in_pool_loop", _fake_run)
    await sn.warm_cache()
    assert sn.is_warmed() is True
    assert sn.code_to_id("A") == 1


async def test_warm_query_error_keeps_fallback(isolate_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", object())

    def _boom(coro):
        coro.close()
        raise RuntimeError("query failed")

    monkeypatch.setattr(db_pool, "run_in_pool_loop", _boom)
    await sn.warm_cache()
    assert sn.is_warmed() is True
    assert sn.code_to_id("A") == 1


# ── warm success ────────────────────────────────────────────────────────────


async def test_warm_success_replaces_cache_and_builds_digest(isolate_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", object())

    # SELECT returns (code, id, name_en, keywords_en).
    rows = [
        ("A", 101, "Athleisure", ["sporty", "techwear", "active", "extra"]),
        ("B", 202, "", []),  # no name → skipped in digest
    ]

    def _fake_run(coro):
        coro.close()
        return rows

    monkeypatch.setattr(db_pool, "run_in_pool_loop", _fake_run)
    await sn.warm_cache()

    assert sn.is_warmed() is True
    # cache replaced with DB ids (not the fallback 1/2).
    assert sn.code_to_id("A") == 101
    assert sn.code_to_id("B") == 202

    nodes = sn.list_nodes()
    assert {n.code for n in nodes} == {"A", "B"}
    a_node = next(n for n in nodes if n.code == "A")
    assert a_node.name_en == "Athleisure"
    assert a_node.keywords_en == ("sporty", "techwear", "active", "extra")

    digest = sn.digest_lines()
    # Only A produces a digest line (B has no name_en); first 3 keywords used.
    assert len(digest) == 1
    assert "A: Athleisure" in digest[0]
    assert "sporty, techwear, active" in digest[0]
