"""EmbedProvider.warm() — keepwarm 전용 Modal GPU 워밍 경로.

check_connection(/health) 대신 실제 임베딩 경로(/embed/text)를 캐시 우회로
호출해 콜드스타트하는 GPU 컨테이너를 데운다.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.embedding import _WARM_CANARY, EmbedProvider


def _mock_client(json_payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_payload)
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_warm_posts_embed_text_canary_and_returns_ms(monkeypatch):
    client = _mock_client({"embedding": [0.1, 0.2, 0.3], "dim": 3})
    monkeypatch.setattr(EmbedProvider, "get_client", classmethod(lambda cls: client))

    ms = await EmbedProvider.warm()

    assert isinstance(ms, int) and ms >= 0
    client.post.assert_awaited_once()
    args, kwargs = client.post.call_args
    assert args[0] == "/embed/text"  # 실제 GPU 임베딩 경로 (not /health)
    assert kwargs["json"] == {"text": _WARM_CANARY}  # 고정 canary


@pytest.mark.asyncio
async def test_warm_bypasses_cache(monkeypatch):
    # warm() 은 embedding_cache 를 절대 참조하지 않아야 한다(캐시 hit 시 Modal 을
    # 스킵하면 워밍이 무의미). get_cached 를 폭발하게 만들어도 warm 은 성공해야.
    import app.providers.embedding as emod

    boom = AsyncMock(side_effect=AssertionError("warm must not touch the cache"))
    monkeypatch.setattr(emod.embedding_cache, "get_cached", boom, raising=False)
    client = _mock_client({"embedding": [0.1, 0.2]})
    monkeypatch.setattr(EmbedProvider, "get_client", classmethod(lambda cls: client))

    ms = await EmbedProvider.warm()
    assert isinstance(ms, int)
    boom.assert_not_awaited()


@pytest.mark.asyncio
async def test_warm_raises_on_bad_response(monkeypatch):
    client = _mock_client({"oops": "no embedding"})
    monkeypatch.setattr(EmbedProvider, "get_client", classmethod(lambda cls: client))

    with pytest.raises(ValueError):
        await EmbedProvider.warm()
