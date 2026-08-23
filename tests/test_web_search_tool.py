"""web_search 툴 dispatch 테스트 — fail-open 계약 + 응답 파싱.

외부 Tavily 호출은 httpx.AsyncClient 를 monkeypatch 해서 대체한다. 어떤 실패에서도
raise 하지 않고 ok=True(results=[]) 로 degrade 하는 것이 핵심 계약.
"""

from __future__ import annotations

from app.agents.tools import web_search
from app.core.config import settings


async def test_empty_query_returns_error(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "k")
    r = await web_search.dispatch({"query": "  "}, {})
    assert r["ok"] is False and r["error"] == "empty_query"


async def test_no_key_fail_open(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    r = await web_search.dispatch({"query": "닝닝 공항패션"}, {})
    assert r["ok"] is True and r["results"] == [] and r["error"] == "disabled"


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload):
        self._p = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        return _FakeResp(self._p)


async def test_parses_answer_and_results(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "k")
    payload = {
        "answer": "닝닝 공항패션은 미니멀+스트릿 무드의 오버사이즈 룩",
        "results": [
            {"title": "T1", "url": "https://a.com", "content": "oversized black tee"},
            {"title": "T2", "url": "https://b.com", "content": "wide denim"},
        ],
    }
    monkeypatch.setattr(web_search.httpx, "AsyncClient", lambda *a, **k: _FakeClient(payload))
    r = await web_search.dispatch({"query": "닝닝 공항패션st"}, {})
    assert r["ok"] is True and r["error"] is None
    assert "미니멀" in r["answer"]
    assert len(r["results"]) == 2 and r["results"][0]["url"] == "https://a.com"


async def test_http_error_fail_open(monkeypatch):
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "k")

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(web_search.httpx, "AsyncClient", _boom)
    r = await web_search.dispatch({"query": "제니st"}, {})
    assert r["ok"] is True and r["results"] == [] and r["error"].startswith("search_failed")


def test_tool_gated_out_when_no_key(monkeypatch):
    # llm_client should NOT advertise web_search when the key is empty.
    from app.agents import llm_client

    monkeypatch.setattr(llm_client.settings, "TAVILY_API_KEY", "")
    names = {t["function"]["name"] for t in llm_client._build_tools_schema()}
    assert "web_search" not in names
    monkeypatch.setattr(llm_client.settings, "TAVILY_API_KEY", "k")
    names2 = {t["function"]["name"] for t in llm_client._build_tools_schema()}
    assert "web_search" in names2
