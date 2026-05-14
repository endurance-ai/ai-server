"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-005 (mode C).

`link_resolver.resolve_batch` — concurrency cap + partial failure tolerance.
Existing single-URL `resolve()` is preserved (characterization re-uses the
existing `tests/test_link_resolver.py` suite indirectly).
"""

from __future__ import annotations

import asyncio

import pytest

from app.channels import link_resolver


@pytest.fixture(autouse=True)
def _clear_cache():
    link_resolver._CACHE.clear()
    yield
    link_resolver._CACHE.clear()


async def test_batch_empty_input_returns_empty():
    out = await link_resolver.resolve_batch([])
    assert out == []


async def test_batch_aggregates_successful_results(monkeypatch):
    async def _fake_resolve(url: str) -> list[str]:
        return [f"img:{url}"]

    monkeypatch.setattr(link_resolver, "resolve", _fake_resolve)
    out = await link_resolver.resolve_batch(["a", "b", "c"])
    assert out == ["img:a", "img:b", "img:c"]


async def test_batch_filters_failures_silently(monkeypatch):
    """Per-URL exceptions and empty results both drop from the output."""

    async def _flaky(url: str) -> list[str]:
        if url == "boom":
            raise RuntimeError("kaboom")
        if url == "empty":
            return []
        return [f"img:{url}"]

    monkeypatch.setattr(link_resolver, "resolve", _flaky)
    out = await link_resolver.resolve_batch(["good1", "boom", "empty", "good2"])
    assert out == ["img:good1", "img:good2"]


async def test_batch_respects_concurrency_cap(monkeypatch):
    """Concurrency limit prevents more than `concurrency` concurrent resolves."""
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def _slow(url: str) -> list[str]:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return [f"img:{url}"]

    monkeypatch.setattr(link_resolver, "resolve", _slow)
    urls = [f"u{i}" for i in range(10)]
    await link_resolver.resolve_batch(urls, concurrency=3)
    assert max_in_flight <= 3


async def test_batch_concurrency_clamped_to_input_size(monkeypatch):
    """Concurrency > len(urls) is harmless — semaphore clamps internally."""

    async def _ok(url: str) -> list[str]:
        return [f"img:{url}"]

    monkeypatch.setattr(link_resolver, "resolve", _ok)
    out = await link_resolver.resolve_batch(["x"], concurrency=99)
    assert out == ["img:x"]
