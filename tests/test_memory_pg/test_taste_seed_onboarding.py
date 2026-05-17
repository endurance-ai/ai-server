"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-SEED-001 + REQ-ONBOARD-MEMORY-AMEND-001.

Postgres-backed `seed_from_onboarding` semantics. Lives in `test_memory_pg/`
so the testcontainers session fixtures are scoped correctly.

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pg_taste_store(pool_initialized):  # type: ignore[no-untyped-def]
    from app.infrastructure.memory.taste_profile_pg import PostgresTasteProfileStore

    return PostgresTasteProfileStore()


def test_postgres_seed_additive_merge(pg_taste_store):
    pg_taste_store.seed_from_onboarding("u:pg", {"minimal": 0.7, "oversize": 0.5})
    p = pg_taste_store.get_or_create("u:pg")
    assert p.liked_keywords.get("minimal") == 0.7
    assert p.liked_keywords.get("oversize") == 0.5


def test_postgres_seed_does_not_overwrite_existing(pg_taste_store):
    p = pg_taste_store.get_or_create("u:pg2")
    p.liked_keywords["existing"] = 0.9
    pg_taste_store.update(p)
    pg_taste_store.seed_from_onboarding("u:pg2", {"new_one": 0.7})
    p2 = pg_taste_store.get_or_create("u:pg2")
    assert p2.liked_keywords["existing"] == 0.9
    assert p2.liked_keywords["new_one"] == 0.7


def test_postgres_seed_caps_per_keyword(pg_taste_store, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ONBOARDING_SEED_MAX_WEIGHT", 0.7, raising=False)
    pg_taste_store.seed_from_onboarding("u:pg3", {"minimal": 99.0})
    p = pg_taste_store.get_or_create("u:pg3")
    assert p.liked_keywords["minimal"] == 0.7


def test_postgres_seed_empty_is_noop(pg_taste_store):
    pg_taste_store.seed_from_onboarding("u:empty", {})
    p = pg_taste_store.get_or_create("u:empty")
    assert p.liked_keywords == {}


def test_postgres_seed_drops_non_positive(pg_taste_store):
    pg_taste_store.seed_from_onboarding("u:zero", {"a": 0.0, "b": -1.0, "c": 0.5})
    p = pg_taste_store.get_or_create("u:zero")
    assert "a" not in p.liked_keywords
    assert "b" not in p.liked_keywords
    assert p.liked_keywords.get("c") == 0.5
