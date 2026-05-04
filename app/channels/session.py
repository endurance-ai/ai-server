"""In-memory session store with per-chat asyncio.Lock + 30-min TTL eviction.

SINGLE-WORKER ASSUMPTION: This store lives in process memory. Running uvicorn with
more than one worker will split sessions across processes and break the state
machine. For the demo we run `--workers 1`. Post-demo migration target is Redis.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class SessionState(StrEnum):
    IDLE = "idle"
    LINK_RESOLUTION = "link_resolution"
    AWAITING_IMAGE_PICK = "awaiting_image_pick"
    VISION_PROCESSING = "vision_processing"
    AWAITING_INTENT = "awaiting_intent"
    SEARCHING = "searching"
    RESULTS_SENT = "results_sent"


def _ttl_seconds() -> float:
    raw = os.getenv("SESSION_TTL_SECONDS", "1800")
    try:
        return float(raw)
    except ValueError:
        return 1800.0


@dataclass
class Session:
    chat_id: int
    state: SessionState = SessionState.IDLE
    image_url: str | None = None
    vision_keywords: list[str] = field(default_factory=list)
    vision_item: str | None = None
    user_intent: str | None = None
    last_active: float = field(default_factory=lambda: time.time())


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[int, Session] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
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


_store: SessionStore | None = None


def get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
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
