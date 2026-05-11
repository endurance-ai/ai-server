"""REQ-MEMORY-PERSIST-001/002/003 + REQ-MEMORY-PROTOCOL-002 (taste delete)."""

from __future__ import annotations

import math
import time

import pytest

from app.channels.taste_profile import TasteProfile
from app.channels.taste_profile_pg import PostgresTasteProfileStore


def test_get_or_create_returns_default_profile():
    store = PostgresTasteProfileStore()
    p = store.get_or_create("u:1")
    assert p.user_key == "u:1"
    assert p.liked_brands == {}
    assert p.disliked_brands == {}


def test_update_then_fresh_read_round_trips_brand_weights():
    """REQ-MEMORY-PERSIST-001 acceptance."""
    store = PostgresTasteProfileStore()
    p = store.get_or_create("u:42")
    p.liked_brands = {"ami": 1.0, "lemaire": 0.9}
    p.liked_keywords = {"oversized": 1.0}
    p.last_active = time.time()
    store.update(p)

    p2 = store.get_or_create("u:42")
    assert p2.liked_brands == {"ami": 1.0, "lemaire": 0.9}
    assert p2.liked_keywords == {"oversized": 1.0}


def test_decay_weights_survive_round_trip_under_isclose():
    """REQ-MEMORY-PERSIST-002 acceptance — 10 reinforces × 0.9 decay."""
    store = PostgresTasteProfileStore()
    p = store.get_or_create("u:decay")
    for _ in range(10):
        p.reinforce_liked_brand("ami", weight=1.0)
    in_mem = dict(p.liked_brands)
    store.update(p)
    p2 = store.get_or_create("u:decay")
    for k, v in in_mem.items():
        assert math.isclose(p2.liked_brands[k], v, rel_tol=1e-9)


def test_last_active_microsecond_round_trip():
    """REQ-MEMORY-PERSIST-002 — `abs(written - read) ≤ 1e-6`."""
    store = PostgresTasteProfileStore()
    p = TasteProfile(user_key="u:ts")
    written = time.time()
    p.last_active = written
    store.update(p)
    p2 = store.get_or_create("u:ts")
    assert abs(p2.last_active - written) <= 1e-6


def test_stale_profile_is_readable_not_evicted():
    """REQ-MEMORY-PERSIST-003 — profile with last_active 60d ago still readable."""
    store = PostgresTasteProfileStore()
    p = TasteProfile(user_key="u:stale", liked_brands={"ami": 1.0})
    p.last_active = time.time() - 60 * 24 * 3600  # 60 days ago
    store.update(p)
    p2 = store.get_or_create("u:stale")
    assert p2.liked_brands == {"ami": 1.0}


def test_delete_idempotent_on_missing_key():
    """REQ-MEMORY-PROTOCOL-002 — delete on non-existent key does not raise."""
    store = PostgresTasteProfileStore()
    store.delete("u:does-not-exist")  # must not raise


def test_delete_twice_silent_second_call():
    """REQ-MEMORY-PROTOCOL-002 — second delete returns silently."""
    store = PostgresTasteProfileStore()
    p = store.get_or_create("u:del-twice")
    store.update(p)
    store.delete("u:del-twice")
    store.delete("u:del-twice")  # silent


def test_cap_enforced_before_write():
    """REQ-MEMORY-PERSIST-002 — cap behavior runs at dataclass layer."""
    store = PostgresTasteProfileStore()
    p = store.get_or_create("u:cap")
    for i in range(60):
        p.reinforce_liked_brand(f"brand{i:02d}", weight=1.0)
    assert len(p.liked_brands) <= 50
    store.update(p)
    p2 = store.get_or_create("u:cap")
    assert len(p2.liked_brands) <= 50


def test_protocol_lock_for_returns_asyncio_lock():
    """REQ-MEMORY-PROTOCOL-001 — surface unchanged."""
    import asyncio as _asyncio

    store = PostgresTasteProfileStore()
    lock = store.lock_for("u:lock")
    assert isinstance(lock, _asyncio.Lock)
    assert store.lock_for("u:lock") is lock


@pytest.mark.asyncio
async def test_start_stop_no_background_task():
    """No background eviction task — REQ-MEMORY-PERSIST-003."""
    store = PostgresTasteProfileStore()
    await store.start()
    await store.stop()  # both no-ops; must not raise
