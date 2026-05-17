"""Dependency-injection container (SPEC-ARCH-AI-001 PR3, REQ-AI-003).

FastAPI `Depends` providers for the DB pool, settings, and the embedding
client. This is the canonical resolution point: request handlers and
services that need a DB pool / settings / embedding client resolve them
via these providers instead of importing module-global state directly.
app.providers.db_pool's accessors become thin delegating adapters to the
providers here (adapter-over-DI), so old call sites keep working
uninterrupted (REQ-AI-003: "db_pool 를 di 컨테이너 위임 어댑터로 유지").

[HARD] Behavior byte-identical (REQ-AI-007). The live pool *state* remains
in app.providers.db_pool's module namespace so that:
  * `db_pool._pool` direct attribute reads (app/main.py conv-log probe) stay
    correct,
  * `monkeypatch.setattr("app.providers.db_pool.get_pool", ...)` string
    patches (tests/test_conversation_log/*) keep intercepting,
  * the dedicated pool event-loop thread semantics are unchanged.
The provider reads db_pool's `_pool` / `_loop` globals (not db_pool.get_pool)
so there is no delegation cycle: db_pool.get_pool -> di.provide_db_pool ->
read db_pool._pool.

Settings reuse the existing `app.core.config.get_settings` lru_cache
singleton (no second instantiation).
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from app.core.config import Settings, get_settings

if TYPE_CHECKING:
    import asyncio

    from psycopg_pool import AsyncConnectionPool

    from app.providers.embedding import EmbedProvider as _EmbedProviderType


def provide_settings() -> Settings:
    """DI provider: application settings singleton (REQ-AI-003)."""
    return get_settings()


def provide_db_pool() -> AsyncConnectionPool:
    """DI provider: the psycopg3 AsyncConnectionPool.

    Reads the live pool state owned by app.providers.db_pool's module
    namespace. Raises the same RuntimeError as the legacy db_pool.get_pool
    when the pool was never initialized (byte-identical message).
    """
    from app.providers import db_pool

    if db_pool._pool is None:  # noqa: SLF001 — state owner is db_pool by design
        raise RuntimeError("db pool not initialized; call init_pool() during lifespan")
    return db_pool._pool  # noqa: SLF001


def provide_db_loop() -> asyncio.AbstractEventLoop:
    """DI provider: the dedicated pool event-loop. Byte-identical error."""
    from app.providers import db_pool

    if db_pool._loop is None:  # noqa: SLF001
        raise RuntimeError("db pool loop not initialized")
    return db_pool._loop  # noqa: SLF001


@lru_cache
def provide_embed_provider() -> type[_EmbedProviderType]:
    """DI provider: the embedding client (Modal FashionSigLIP wrapper).

    EmbedProvider is a classmethod-based singleton; the type itself is the
    injectable handle (byte-identical to importing the class directly).
    """
    from app.providers.embedding import EmbedProvider

    return EmbedProvider


__all__ = [
    "provide_settings",
    "provide_db_pool",
    "provide_db_loop",
    "provide_embed_provider",
]
