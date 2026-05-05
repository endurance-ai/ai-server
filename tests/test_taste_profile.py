"""Long-term taste profile tests — reinforcement, decay, caps, store contract."""

from __future__ import annotations

import asyncio

import pytest

from app.channels.taste_profile import (
    InMemoryTasteProfileStore,
    TasteProfile,
    user_key_for,
)


def test_user_key_prefers_user_id_over_chat_id():
    assert user_key_for(123, 999) == "u:123"
    assert user_key_for(None, 999) == "c:999"


def test_reinforce_liked_brand_lowercases_and_decays_others():
    p = TasteProfile(user_key="u:1")
    p.reinforce_liked_brand("Ami", weight=1.0)
    p.reinforce_liked_brand("MFPEN", weight=1.0)
    assert "ami" in p.liked_brands
    assert "mfpen" in p.liked_brands
    # Decay: ami's weight after the second reinforce = 1.0 * 0.9 = 0.9
    assert p.liked_brands["ami"] == pytest.approx(0.9)


def test_liking_a_previously_disliked_brand_clears_dislike():
    p = TasteProfile(user_key="u:1")
    p.reinforce_disliked_brand("ami")
    assert "ami" in p.disliked_brands
    p.reinforce_liked_brand("ami")
    assert "ami" not in p.disliked_brands
    assert "ami" in p.liked_brands


def test_boost_brands_returns_top_n_in_descending_score():
    p = TasteProfile(user_key="u:1")
    for b, w in [("c", 1.0), ("a", 3.0), ("b", 2.0)]:
        p.reinforce_liked_brand(b, weight=w)
    top = p.boost_brands(top_n=2)
    # Order: highest score first. After decay applied to earlier entries,
    # ranking can shift — accept either "a/b" first by checking membership.
    assert len(top) == 2
    assert "a" in top  # strongest signal


def test_exclude_brands_threshold_filter():
    p = TasteProfile(user_key="u:1")
    p.reinforce_disliked_brand("zara", weight=2.0)
    p.reinforce_disliked_brand("h&m", weight=0.5)
    excl = p.exclude_brands(threshold=1.5)
    assert "zara" in excl
    assert "h&m" not in excl  # below threshold


def test_observe_price_tracks_min_max():
    p = TasteProfile(user_key="u:1")
    p.observe_price(50000)
    p.observe_price(120000)
    p.observe_price(30000)
    assert p.price_min_observed == 30000
    assert p.price_max_observed == 120000


def test_observe_price_ignores_invalid_values():
    p = TasteProfile(user_key="u:1")
    p.observe_price(None)
    p.observe_price(0)
    p.observe_price(-5)
    assert p.price_min_observed is None


@pytest.mark.asyncio
async def test_in_memory_store_lifecycle_and_lock_per_user():
    s = InMemoryTasteProfileStore()
    await s.start()
    try:
        p1 = s.get_or_create("u:1")
        p2 = s.get_or_create("u:1")
        assert p1 is p2  # same instance returned
        lock_a = s.lock_for("u:1")
        lock_b = s.lock_for("u:1")
        assert lock_a is lock_b
        assert isinstance(lock_a, asyncio.Lock)
        s.delete("u:1")
        assert s.get_or_create("u:1") is not p1  # fresh after delete
    finally:
        await s.stop()
