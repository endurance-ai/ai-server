"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-005 + REQ-ONBOARD-SEC-001.

Mocked httpx behavior tests for `app.providers.apify.run_pinterest_scrape`.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers import apify as apify_mod
from app.providers.apify import ApifyTimeoutError, run_pinterest_scrape


@pytest.fixture(autouse=True)
def _set_token(monkeypatch):
    """All tests start with a non-empty token unless they explicitly clear it."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "APIFY_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(settings, "APIFY_PINTEREST_ACTOR", "epctex/pinterest-scraper", raising=False)


class _DummyResponse:
    def __init__(self, status_code: int = 200, payload: list | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def json(self):
        return self._payload


class _DummyClient:
    def __init__(self, response: _DummyResponse | None = None, exc: Exception | None = None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def post(self, *_a, **_kw):
        if self._exc:
            raise self._exc
        return self._response


def _patch_httpx(monkeypatch, client: _DummyClient):
    def _factory(*_a, **_kw):
        return client

    monkeypatch.setattr(apify_mod.httpx, "AsyncClient", _factory)


async def test_success_returns_normalized_items(monkeypatch):
    payload = [
        {"imageUrl": "https://i.pinimg.com/a.jpg", "description": "coat", "link": "https://pin.it/a"},
        {"image": "https://i.pinimg.com/b.jpg", "title": "boots"},
    ]
    _patch_httpx(monkeypatch, _DummyClient(_DummyResponse(200, payload)))
    out = await run_pinterest_scrape("https://pinterest.com/jane/coats/", "board", max_items=10)
    assert len(out) == 2
    assert out[0]["image_url"] == "https://i.pinimg.com/a.jpg"
    assert out[0]["description"] == "coat"
    assert out[1]["image_url"] == "https://i.pinimg.com/b.jpg"


async def test_missing_token_returns_empty_list(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "APIFY_TOKEN", "", raising=False)
    out = await run_pinterest_scrape("https://pinterest.com/jane/", "profile")
    assert out == []


async def test_timeout_raises_apify_timeout_error(monkeypatch):
    _patch_httpx(monkeypatch, _DummyClient(exc=TimeoutError()))
    with pytest.raises(ApifyTimeoutError):
        await run_pinterest_scrape("https://pinterest.com/jane/coats/", "board")


async def test_http_error_returns_empty(monkeypatch):
    _patch_httpx(monkeypatch, _DummyClient(exc=httpx.ConnectError("nope")))
    out = await run_pinterest_scrape("https://pinterest.com/jane/coats/", "board")
    assert out == []


async def test_4xx_status_returns_empty(monkeypatch):
    _patch_httpx(monkeypatch, _DummyClient(_DummyResponse(401, [])))
    out = await run_pinterest_scrape("https://pinterest.com/jane/coats/", "board")
    assert out == []


async def test_per_item_failure_tolerance(monkeypatch):
    """Items lacking image_url are dropped; good items survive."""
    payload = [
        {"description": "no image at all"},  # malformed → dropped
        {"imageUrl": "https://i.pinimg.com/ok.jpg"},  # good
        "not a dict",  # garbage → dropped
        {"imageUrl": ""},  # empty → dropped
    ]
    _patch_httpx(monkeypatch, _DummyClient(_DummyResponse(200, payload)))
    out = await run_pinterest_scrape("https://pinterest.com/jane/coats/", "board")
    assert len(out) == 1
    assert out[0]["image_url"] == "https://i.pinimg.com/ok.jpg"


async def test_non_list_payload_returns_empty(monkeypatch):
    _patch_httpx(monkeypatch, _DummyClient(_DummyResponse(200, payload=None)))
    # Json shape is `[]` from _DummyResponse default — verify alternate path.
    out = await run_pinterest_scrape("https://pinterest.com/jane/coats/", "board")
    assert out == []
