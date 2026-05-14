"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-SEED-001 + REQ-ONBOARD-MEMORY-AMEND-001.

`TasteProfileStore.seed_from_onboarding` additive-merge semantics across both
backends. Postgres path goes through the testcontainers fixture in
`tests/test_memory_pg/conftest.py`.
"""

from __future__ import annotations

from app.channels.taste_profile import InMemoryTasteProfileStore


def test_inmemory_seed_adds_new_keywords():
    store = InMemoryTasteProfileStore()
    store.seed_from_onboarding("u:1", {"minimal": 0.7, "oversize": 0.7})
    p = store.get_or_create("u:1")
    assert p.liked_keywords.get("minimal") == 0.7
    assert p.liked_keywords.get("oversize") == 0.7


def test_inmemory_seed_additive_does_not_overwrite():
    """Existing weights survive — seed adds, never overwrites."""
    store = InMemoryTasteProfileStore()
    p = store.get_or_create("u:2")
    p.liked_keywords["existing_kw"] = 0.5
    store.update(p)
    store.seed_from_onboarding("u:2", {"new_kw": 0.7})
    p2 = store.get_or_create("u:2")
    assert p2.liked_keywords["existing_kw"] == 0.5  # untouched
    assert p2.liked_keywords["new_kw"] == 0.7


def test_inmemory_seed_caps_at_max_weight(monkeypatch):
    """REQ-ONBOARD-SEED-002 — per-keyword cap (0.7 default)."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "ONBOARDING_SEED_MAX_WEIGHT", 0.7, raising=False)
    store = InMemoryTasteProfileStore()
    store.seed_from_onboarding("u:cap", {"minimal": 5.0})  # excessive
    p = store.get_or_create("u:cap")
    assert p.liked_keywords["minimal"] == 0.7  # capped


def test_inmemory_seed_repeated_accumulates_capped():
    """Two calls each contribute ≤ cap, so total = 2 * cap max."""
    store = InMemoryTasteProfileStore()
    store.seed_from_onboarding("u:acc", {"oversize": 0.7})
    store.seed_from_onboarding("u:acc", {"oversize": 0.7})
    p = store.get_or_create("u:acc")
    assert abs(p.liked_keywords["oversize"] - 1.4) < 1e-9


def test_inmemory_seed_drops_non_positive_weights():
    store = InMemoryTasteProfileStore()
    store.seed_from_onboarding("u:zero", {"a": 0.0, "b": -0.5, "c": 0.5})
    p = store.get_or_create("u:zero")
    assert "a" not in p.liked_keywords
    assert "b" not in p.liked_keywords
    assert p.liked_keywords.get("c") == 0.5


def test_inmemory_seed_empty_is_noop():
    store = InMemoryTasteProfileStore()
    store.seed_from_onboarding("u:empty", {})
    p = store.get_or_create("u:empty")
    assert p.liked_keywords == {}


def test_inmemory_seed_strips_and_lowercases_keys():
    store = InMemoryTasteProfileStore()
    store.seed_from_onboarding("u:norm", {"  Minimal  ": 0.5})
    p = store.get_or_create("u:norm")
    assert p.liked_keywords.get("minimal") == 0.5


def test_inmemory_seed_removes_from_disliked():
    """If kw appears in disliked, seeding to liked should remove the disliked entry."""
    store = InMemoryTasteProfileStore()
    p = store.get_or_create("u:flip")
    p.disliked_keywords["minimal"] = 0.5
    store.update(p)
    store.seed_from_onboarding("u:flip", {"minimal": 0.7})
    p2 = store.get_or_create("u:flip")
    assert "minimal" not in p2.disliked_keywords
    assert p2.liked_keywords.get("minimal") == 0.7


# Postgres-backed tests live alongside the testcontainers infrastructure in
# `tests/test_memory_pg/test_taste_seed_pg.py` to avoid pulling the docker-bound
# session fixture into the lightweight onboarding test directory.
