from unittest.mock import AsyncMock, patch

from httpx import AsyncClient


async def test_health_liveness(client: AsyncClient):
    """GET /health 는 무인증 + 부울만 반환."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "version" in data
    # 인프라 토폴로지(database/modal/litellm) 가 노출되지 않아야 함
    assert "database" not in data
    assert "modal_embed" not in data
    assert "litellm" not in data


async def test_health_ready_all_connected(client: AsyncClient):
    """/health/ready — DB + Modal + LiteLLM 모두 연결되면 status=ok."""
    with (
        patch("app.api.health.DatabaseProvider.check_connection", new_callable=AsyncMock, return_value=True),
        patch("app.api.health.EmbedProvider.check_connection", new_callable=AsyncMock, return_value=True),
        patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=True),
    ):
        resp = await client.get("/health/ready")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"
    assert data["modal_embed"] == "connected"
    assert data["litellm"] == "connected"


async def test_health_ready_database_down(client: AsyncClient):
    with (
        patch("app.api.health.DatabaseProvider.check_connection", new_callable=AsyncMock, return_value=False),
        patch("app.api.health.EmbedProvider.check_connection", new_callable=AsyncMock, return_value=True),
        patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=True),
    ):
        resp = await client.get("/health/ready")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["database"] == "disconnected"


async def test_health_ready_modal_down(client: AsyncClient):
    with (
        patch("app.api.health.DatabaseProvider.check_connection", new_callable=AsyncMock, return_value=True),
        patch("app.api.health.EmbedProvider.check_connection", new_callable=AsyncMock, return_value=False),
        patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=True),
    ):
        resp = await client.get("/health/ready")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["modal_embed"] == "disconnected"


async def test_health_ready_litellm_down(client: AsyncClient):
    with (
        patch("app.api.health.DatabaseProvider.check_connection", new_callable=AsyncMock, return_value=True),
        patch("app.api.health.EmbedProvider.check_connection", new_callable=AsyncMock, return_value=True),
        patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=False),
    ):
        resp = await client.get("/health/ready")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["litellm"] == "disconnected"


async def test_health_ready_requires_token_when_set(client: AsyncClient, monkeypatch):
    """INTERNAL_API_TOKEN 설정 시 /health/ready 는 토큰 없으면 401."""
    from app.core import auth as auth_mod

    monkeypatch.setattr(auth_mod.settings, "INTERNAL_API_TOKEN", "secret-token")
    resp = await client.get("/health/ready")
    assert resp.status_code == 401

    resp_ok = await client.get("/health/ready", headers={"X-Internal-Token": "secret-token"})
    # 의존성 체크는 통과 — providers 가 실 연결을 시도해서 503 나오는 게 정상
    assert resp_ok.status_code in (200, 503)
