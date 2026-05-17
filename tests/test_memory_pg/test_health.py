"""REQ-MEMORY-HEALTH-001 — /health/ready exposes memory_backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.infrastructure.memory import session as session_mod
from app.infrastructure.memory import taste_profile as taste_mod
from app.infrastructure.memory.session import InMemorySessionStore
from app.infrastructure.memory.session_pg import PostgresSessionStore
from app.infrastructure.memory.taste_profile import InMemoryTasteProfileStore


@pytest.mark.asyncio
async def test_health_ready_reports_postgres_backend():
    from app.main import app

    # Inject the Postgres store so health reports postgres
    session_mod.set_store(PostgresSessionStore())
    try:
        with (
            patch("app.api.health.SupabaseProvider.check_connection", new_callable=AsyncMock, return_value=True),
            patch("app.api.health.EmbedProvider.check_connection", new_callable=AsyncMock, return_value=True),
            patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=True),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health/ready")
        assert resp.status_code in (200, 503)
        assert resp.json()["memory_backend"] == "postgres"
    finally:
        session_mod._store = None
        taste_mod._store = None


@pytest.mark.asyncio
async def test_health_ready_reports_in_memory_backend():
    from app.main import app

    session_mod.set_store(InMemorySessionStore())
    taste_mod.set_taste_store(InMemoryTasteProfileStore())
    try:
        with (
            patch("app.api.health.SupabaseProvider.check_connection", new_callable=AsyncMock, return_value=True),
            patch("app.api.health.EmbedProvider.check_connection", new_callable=AsyncMock, return_value=True),
            patch("app.api.health.LLMProvider.check_connection", new_callable=AsyncMock, return_value=True),
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/health/ready")
        assert resp.status_code in (200, 503)
        assert resp.json()["memory_backend"] == "in_memory"
    finally:
        session_mod._store = None
        taste_mod._store = None
