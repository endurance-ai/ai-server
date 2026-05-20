"""Long-term per-user taste profile.

Separate from `Session` (per-conversation working memory). A `TasteProfile`
follows the *user* across conversations and is built up implicitly from
critique signals (More like / Less like) and explicitly from `taste_update`
intents ("나 ami 좋아해").

Storage shape mirrors `SessionStore`: a Protocol + an in-memory default,
swappable to Redis later without touching scenario logic.

Decay: scores are multiplied by 0.9 on update of any key, so older signals
gradually fade. (No background task — decay is lazy on write.)
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)

_DECAY = 0.9
_MAX_BRANDS = 50
_MAX_KEYWORDS = 50


@dataclass
class TasteProfile:
    user_key: str  # "u:{from_user_id}" or fallback "c:{chat_id}"
    liked_brands: dict[str, float] = field(default_factory=dict)
    disliked_brands: dict[str, float] = field(default_factory=dict)
    liked_keywords: dict[str, float] = field(default_factory=dict)
    disliked_keywords: dict[str, float] = field(default_factory=dict)
    price_min_observed: int | None = None
    price_max_observed: int | None = None
    last_active: float = field(default_factory=lambda: time.time())
    # SPEC-AGENT-V3-REACT Gap4 — additive cross-thread dislike timestamps.
    # Per-key single last-dislike epoch seconds (OQ-V3-3 resolved). Default
    # empty so old serialized rows deserialize without KeyError. The existing
    # disliked_* score dicts + reinforce_disliked_* semantics are UNCHANGED.
    # @MX:SPEC: SPEC-AGENT-V3-REACT
    disliked_brands_ts: dict[str, float] = field(default_factory=dict)
    disliked_keywords_ts: dict[str, float] = field(default_factory=dict)

    def reinforce_liked_brand(self, brand: str, weight: float = 1.0) -> None:
        if not brand:
            return
        b = brand.strip().lower()
        for d in (self.liked_brands, self.disliked_brands):
            for k in list(d):
                d[k] *= _DECAY
        self.liked_brands[b] = self.liked_brands.get(b, 0.0) + weight
        self.disliked_brands.pop(b, None)
        self._cap()

    def reinforce_disliked_brand(self, brand: str, weight: float = 1.0) -> None:
        if not brand:
            return
        b = brand.strip().lower()
        for d in (self.liked_brands, self.disliked_brands):
            for k in list(d):
                d[k] *= _DECAY
        self.disliked_brands[b] = self.disliked_brands.get(b, 0.0) + weight
        self.liked_brands.pop(b, None)
        self._cap()

    def reinforce_liked_keywords(self, keywords: list[str], weight: float = 1.0) -> None:
        for k in (k.strip().lower() for k in keywords if k):
            for d in (self.liked_keywords, self.disliked_keywords):
                for kk in list(d):
                    d[kk] *= _DECAY
            self.liked_keywords[k] = self.liked_keywords.get(k, 0.0) + weight
            self.disliked_keywords.pop(k, None)
        self._cap()

    def reinforce_disliked_keywords(self, keywords: list[str], weight: float = 1.0) -> None:
        for k in (k.strip().lower() for k in keywords if k):
            for d in (self.liked_keywords, self.disliked_keywords):
                for kk in list(d):
                    d[kk] *= _DECAY
            self.disliked_keywords[k] = self.disliked_keywords.get(k, 0.0) + weight
            self.liked_keywords.pop(k, None)
        self._cap()

    def observe_price(self, price: int | None) -> None:
        if price is None or price <= 0:
            return
        self.price_min_observed = price if self.price_min_observed is None else min(self.price_min_observed, price)
        self.price_max_observed = price if self.price_max_observed is None else max(self.price_max_observed, price)

    def boost_brands(self, top_n: int = 5) -> list[str]:
        return [b for b, _ in sorted(self.liked_brands.items(), key=lambda kv: -kv[1])[:top_n]]

    def exclude_brands(self, threshold: float = 1.5) -> list[str]:
        return [b for b, s in self.disliked_brands.items() if s >= threshold]

    def boost_keywords(self, top_n: int = 5) -> list[str]:
        return [k for k, _ in sorted(self.liked_keywords.items(), key=lambda kv: -kv[1])[:top_n]]

    def recency_weighted_excludes(
        self, now: float, *, half_life_s: float = 60 * 60 * 24 * 30
    ) -> tuple[list[str], list[str]]:
        """SPEC-AGENT-V3-REACT Gap4 — recency-weighted dislike exclude lists.

        Returns (exclude_brands, exclude_keywords). Reuses the EXISTING
        `exclude_brands()` (score>=1.5) as the base brand set, then layers an
        exponential recency weight on the timestamp dicts (OQ-V3-7 — consistent
        with the dataclass's existing 0.9 exponential decay philosophy):

            recency = 0.5 ** ((now - ts) / half_life_s)   # ∈ (0, 1]

        A dislike still in the score-threshold base is always excluded; a
        recently-timestamped dislike (recency >= 0.5, i.e. within one
        half-life) is also excluded even if its score has decayed below 1.5.
        Keyword excludes are derived purely from recency (no prior V2 keyword
        exclude method to reuse — additive, no new ranking). half_life_s
        defaults to the 30-day taste TTL so this never out-lives the profile.

        @MX:NOTE: [AUTO] additive — reuses exclude_brands(), no schema break,
          no new search/ranking algorithm.
        @MX:SPEC: SPEC-AGENT-V3-REACT
        """
        hl = max(1.0, float(half_life_s))

        def _recent(ts: float) -> bool:
            return math.pow(0.5, max(0.0, now - float(ts)) / hl) >= 0.5

        base_brands = set(self.exclude_brands())
        for b, ts in self.disliked_brands_ts.items():
            if _recent(ts):
                base_brands.add(b)
        ex_keywords = [k for k, ts in self.disliked_keywords_ts.items() if _recent(ts)]
        return sorted(base_brands), ex_keywords

    def _cap(self) -> None:
        if len(self.liked_brands) > _MAX_BRANDS:
            self.liked_brands = dict(sorted(self.liked_brands.items(), key=lambda kv: -kv[1])[:_MAX_BRANDS])
        if len(self.disliked_brands) > _MAX_BRANDS:
            self.disliked_brands = dict(sorted(self.disliked_brands.items(), key=lambda kv: -kv[1])[:_MAX_BRANDS])
        if len(self.liked_keywords) > _MAX_KEYWORDS:
            self.liked_keywords = dict(sorted(self.liked_keywords.items(), key=lambda kv: -kv[1])[:_MAX_KEYWORDS])
        if len(self.disliked_keywords) > _MAX_KEYWORDS:
            self.disliked_keywords = dict(sorted(self.disliked_keywords.items(), key=lambda kv: -kv[1])[:_MAX_KEYWORDS])
        # SPEC-AGENT-V3-REACT Gap4 — keep ts dicts bounded too. No-op when the
        # Gap4 flag is off (dicts stay empty → byte-identical V2 _cap result).
        if len(self.disliked_brands_ts) > _MAX_BRANDS:
            self.disliked_brands_ts = dict(sorted(self.disliked_brands_ts.items(), key=lambda kv: -kv[1])[:_MAX_BRANDS])
        if len(self.disliked_keywords_ts) > _MAX_KEYWORDS:
            self.disliked_keywords_ts = dict(
                sorted(self.disliked_keywords_ts.items(), key=lambda kv: -kv[1])[:_MAX_KEYWORDS]
            )


def user_key_for(from_user_id: int | None, chat_id: int) -> str:
    return f"u:{from_user_id}" if from_user_id else f"c:{chat_id}"


class TasteProfileStore(Protocol):
    def get_or_create(self, user_key: str) -> TasteProfile: ...
    def update(self, profile: TasteProfile) -> None: ...
    def delete(self, user_key: str) -> None: ...
    def lock_for(self, user_key: str) -> asyncio.Lock: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class InMemoryTasteProfileStore:
    def __init__(self) -> None:
        self._profiles: dict[str, TasteProfile] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._evict_task: asyncio.Task | None = None

    def get_or_create(self, user_key: str) -> TasteProfile:
        p = self._profiles.get(user_key)
        if p is None:
            p = TasteProfile(user_key=user_key)
            self._profiles[user_key] = p
        return p

    def update(self, profile: TasteProfile) -> None:
        profile.last_active = time.time()
        self._profiles[profile.user_key] = profile

    def delete(self, user_key: str) -> None:
        self._profiles.pop(user_key, None)
        self._locks.pop(user_key, None)

    def lock_for(self, user_key: str) -> asyncio.Lock:
        lock = self._locks.get(user_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_key] = lock
        return lock

    async def _evict_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3600.0)
                ttl = float(settings.TASTE_PROFILE_TTL_SECONDS)
                now = time.time()
                expired = [k for k, p in self._profiles.items() if now - p.last_active > ttl]
                for k in expired:
                    self.delete(k)
                if expired:
                    logger.info("taste_profile_store evicted=%d", len(expired))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("taste_profile_store evict loop error")

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


_StoreFactory = Callable[[], TasteProfileStore]
_store: TasteProfileStore | None = None
_factory: _StoreFactory = InMemoryTasteProfileStore


def set_taste_store_factory(factory: _StoreFactory) -> None:
    global _factory
    _factory = factory


def set_taste_store(store: TasteProfileStore) -> None:
    global _store
    _store = store


def get_taste_store() -> TasteProfileStore:
    global _store
    if _store is None:
        _store = _factory()
    return _store


async def init_taste_store() -> TasteProfileStore:
    store = get_taste_store()
    await store.start()
    return store


async def shutdown_taste_store() -> None:
    global _store
    if _store is not None:
        await _store.stop()
        _store = None
