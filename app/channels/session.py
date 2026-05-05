"""Session store abstraction.

The store is split into a `SessionStore` Protocol (the contract scenario.py
depends on) and an `InMemorySessionStore` implementation. Swapping to Redis
later only requires writing a second implementation and registering it via
`set_store` / `set_store_factory` — scenario logic stays untouched.

SINGLE-WORKER ASSUMPTION (in-memory impl only): The InMemorySessionStore
lives in process memory. Running uvicorn with more than one worker will
split sessions across processes and break the state machine. For the demo
we run `--workers 1`. Post-demo migration target is Redis.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)


class SessionState(StrEnum):
    IDLE = "idle"
    LINK_RESOLUTION = "link_resolution"
    AWAITING_IMAGE_PICK = "awaiting_image_pick"
    VISION_PROCESSING = "vision_processing"
    AWAITING_ITEM_PICK = "awaiting_item_pick"
    AWAITING_INTENT = "awaiting_intent"
    SEARCHING = "searching"
    RESULTS_SENT = "results_sent"


def _ttl_seconds() -> float:
    return float(settings.SESSION_TTL_SECONDS)


@dataclass
class Session:
    chat_id: int
    state: SessionState = SessionState.IDLE
    image_url: str | None = None
    detected_items: list[dict] = field(default_factory=list)
    selected_item_index: int | None = None
    vision_keywords: list[str] = field(default_factory=list)
    vision_item: str | None = None
    user_intent: str | None = None
    last_active: float = field(default_factory=lambda: time.time())


class SessionStore(Protocol):
    """Backend-agnostic session store contract.

    Distributed implementations (Redis, etc.) must satisfy this surface so
    scenario.py keeps working without changes. `lock_for` returns an
    asyncio.Lock for in-process serialization; distributed impls may return
    a lock backed by a remote primitive that exposes the same async context
    manager protocol.
    """

    def get_or_create(self, chat_id: int) -> Session: ...
    def update(self, session: Session) -> None: ...
    def delete(self, chat_id: int) -> None: ...
    def lock_for(self, chat_id: int) -> asyncio.Lock: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class InMemorySessionStore:
    """Single-process in-memory implementation. Default factory."""

    def __init__(self) -> None:
        self._sessions: dict[int, Session] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._evict_task: asyncio.Task | None = None

    def get_or_create(self, chat_id: int) -> Session:
        s = self._sessions.get(chat_id)
        if s is None:
            s = Session(chat_id=chat_id)
            self._sessions[chat_id] = s
        return s

    def update(self, session: Session) -> None:
        session.last_active = time.time()
        self._sessions[session.chat_id] = session

    def delete(self, chat_id: int) -> None:
        self._sessions.pop(chat_id, None)
        self._locks.pop(chat_id, None)

    def lock_for(self, chat_id: int) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    async def _evict_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60.0)
                ttl = _ttl_seconds()
                now = time.time()
                expired = [cid for cid, s in self._sessions.items() if now - s.last_active > ttl]
                for cid in expired:
                    self.delete(cid)
                if expired:
                    logger.info("session_store evicted=%d", len(expired))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("session_store evict loop error")

    async def start(self) -> None:
        if self._evict_task is None:
            self._evict_task = asyncio.create_task(self._evict_loop())

    async def stop(self) -> None:
        if self._evict_task is not None:
            self._evict_task.cancel()
            try:
                await self._evict_task
            except (asyncio.CancelledError, Exception):
                pass
            self._evict_task = None


_StoreFactory = Callable[[], SessionStore]

_store: SessionStore | None = None
_factory: _StoreFactory = InMemorySessionStore


def set_store_factory(factory: _StoreFactory) -> None:
    """Override the factory used by `get_store()`. Use this to register a
    Redis-backed implementation at app startup before the first call."""
    global _factory
    _factory = factory


def set_store(store: SessionStore) -> None:
    """Inject a fully constructed store (mostly for tests)."""
    global _store
    _store = store


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = _factory()
    return _store


async def init_store() -> SessionStore:
    store = get_store()
    await store.start()
    return store


async def shutdown_store() -> None:
    global _store
    if _store is not None:
        await _store.stop()
        _store = None
