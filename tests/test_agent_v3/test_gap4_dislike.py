"""SPEC-AGENT-V3-REACT / T9 — Gap4 cross-thread dislike memory.

REQ-AGENT-V3-DISLIKE-SCHEMA-001 / -DISLIKE-DISCOUNT-001 / -DISLIKE-FLAG-001.
AC-4.1 (ts record + serialize compat), AC-4.2 (cross-thread discount +
recency weight), AC-4.3 (flag OFF byte-identical), E6 (dislike→like pop).
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.infrastructure.memory.taste_profile import TasteProfile

# Taste-store reset + settings snapshot/restore handled centrally by
# tests/test_agent_v3/conftest.py::_v3_isolation (autouse).


# ── AC-4.1 — ts record + serialization compat ──────────────────────────────


@pytest.mark.asyncio
async def test_ac_4_1_records_ts_and_score(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools.update_taste import dispatch
    from app.infrastructure.memory.taste_profile import get_taste_store

    r = await dispatch({"source": "free_text", "brand_dislikes": ["Zara"]}, {"user_key": "u:7"})
    assert r["ok"] is True
    prof = get_taste_store().get_or_create("u:7")
    assert "zara" in prof.disliked_brands_ts  # ts recorded (lowercased)
    assert prof.disliked_brands["zara"] >= 1.0  # V2 score still accrues


def test_ac_4_1_serialization_compat_and_old_row_load():
    """New profile serializes via Jsonb-compatible dicts; old short tuple loads
    with empty ts dicts (no KeyError) — REQ-AGENT-V3-DISLIKE-SCHEMA-001."""
    from app.infrastructure.memory.taste_profile_pg import _row_to_profile

    prof = TasteProfile(user_key="u:7", disliked_brands_ts={"zara": 123.0})
    # Both new dicts are json-serializable.
    json.dumps(prof.disliked_brands_ts)
    json.dumps(prof.disliked_keywords_ts)

    # Pre-migration 8-col row (no ts columns) → defaults to {} (no IndexError).
    old_row = ("u:9", {}, {}, {}, {}, None, None, None)
    p = _row_to_profile(old_row)
    assert p.disliked_brands_ts == {}
    assert p.disliked_keywords_ts == {}

    # Migrated 10-col row round-trips.
    new_row = ("u:9", {}, {}, {}, {}, None, None, None, {"gucci": 5.0}, {"logo": 6.0})
    p2 = _row_to_profile(new_row)
    assert p2.disliked_brands_ts == {"gucci": 5.0}
    assert p2.disliked_keywords_ts == {"logo": 6.0}


# ── AC-4.2 — cross-thread discount + recency weight ────────────────────────


def test_ac_4_2_recency_weighted_excludes_helper():
    """Recent dislike excluded even with decayed score; very old dislike with
    sub-threshold score is NOT (recency expired)."""
    now = time.time()
    prof = TasteProfile(user_key="u:1")
    # gucci: recent ts, low score (< 1.5 threshold) → still excluded by recency.
    prof.disliked_brands["gucci"] = 0.5
    prof.disliked_brands_ts["gucci"] = now - 60  # 1 min ago
    # zara: old ts (> 1 half-life), low score → NOT excluded.
    prof.disliked_brands["zara"] = 0.5
    prof.disliked_brands_ts["zara"] = now - (60 * 60 * 24 * 90)  # 90 days
    # keyword recency-only path.
    prof.disliked_keywords_ts["neon"] = now - 60

    ex_brands, ex_keywords = prof.recency_weighted_excludes(now)
    assert "gucci" in ex_brands
    assert "zara" not in ex_brands
    assert "neon" in ex_keywords


def test_ac_4_2_score_threshold_brand_always_excluded():
    """A dislike whose SCORE >= 1.5 (existing exclude_brands base) is always
    excluded regardless of ts — reuses exclude_brands(), no new ranking."""
    now = time.time()
    prof = TasteProfile(user_key="u:1")
    prof.disliked_brands["prada"] = 2.0  # >= 1.5 base threshold
    ex_brands, _ = prof.recency_weighted_excludes(now)
    assert "prada" in ex_brands


@pytest.mark.asyncio
async def test_ac_4_2_cross_thread_search_discount(monkeypatch):
    """thread A dislike gucci → thread B search drops gucci candidates."""
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools.search_products import apply_dislike_discount
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:7")
    prof.disliked_brands_ts["gucci"] = time.time()
    get_taste_store().update(prof)

    cands = [
        SimpleNamespace(brand="Gucci", name="bag", title="bag"),
        SimpleNamespace(brand="Ami", name="tote", title="tote"),
    ]
    out = apply_dislike_discount({"user_key": "u:7"}, cands)
    assert [c.brand for c in out] == ["Ami"]  # gucci discounted


# ── AC-4.3 — flag OFF byte-identical ───────────────────────────────────────


@pytest.mark.asyncio
async def test_ac_4_3_flag_off_no_ts_record(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", False, raising=False)
    from app.agents.tools.update_taste import dispatch
    from app.infrastructure.memory.taste_profile import get_taste_store

    await dispatch({"source": "free_text", "brand_dislikes": ["Zara"]}, {"user_key": "u:7"})
    prof = get_taste_store().get_or_create("u:7")
    assert prof.disliked_brands_ts == {}  # flag OFF → no ts
    assert prof.disliked_brands["zara"] >= 1.0  # V2 score path unchanged


def test_ac_4_3_flag_off_discount_noop(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", False, raising=False)
    from app.agents.tools.search_products import apply_dislike_discount
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:7")
    prof.disliked_brands_ts["gucci"] = time.time()
    get_taste_store().update(prof)
    cands = [SimpleNamespace(brand="Gucci", name="bag", title="bag")]
    out = apply_dislike_discount({"user_key": "u:7"}, cands)
    assert out is cands  # exact same list object — byte-identical no-op


# ── E6 — dislike then like keeps reinforce_* pop semantics; ts is last only ─


@pytest.mark.asyncio
async def test_e6_dislike_then_like_pop_semantics(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools.update_taste import dispatch
    from app.infrastructure.memory.taste_profile import get_taste_store

    await dispatch({"source": "free_text", "brand_dislikes": ["ami"]}, {"user_key": "u:7"})
    await dispatch({"source": "free_text", "brand_likes": ["ami"]}, {"user_key": "u:7"})
    prof = get_taste_store().get_or_create("u:7")
    # Existing reinforce_* semantics: like pops the brand from disliked.
    assert "ami" not in prof.disliked_brands
    assert "ami" in prof.liked_brands
    # ts dict still carries the last dislike epoch (no contradictory new signal).
    assert "ami" in prof.disliked_brands_ts


# ── BLOCKING-2 — update_taste.py branch coverage ───────────────────────────


@pytest.mark.asyncio
async def test_update_taste_invalid_source():
    """update_taste L23-26 — bad source → invalid_source, not applied."""
    from app.agents.tools.update_taste import dispatch

    r = await dispatch({"source": "bogus", "brand_dislikes": ["zara"]}, {"user_key": "u:7"})
    assert r["ok"] is False
    assert r["error"] == "invalid_source:bogus"
    assert r["applied"] is False


@pytest.mark.asyncio
async def test_update_taste_missing_user_key():
    """update_taste L27-30 — ctx without user_key → missing_user_key."""
    from app.agents.tools.update_taste import dispatch

    r = await dispatch({"source": "free_text", "brand_likes": ["ami"]}, {})
    assert r["ok"] is False
    assert r["error"] == "missing_user_key"


@pytest.mark.asyncio
async def test_update_taste_keyword_likes_and_dislikes_flag_on(monkeypatch):
    """update_taste L47/L49 + L61-63 — keyword likes/dislikes branches AND
    the Gap4 flag-ON keyword-dislike timestamp record path."""
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools.update_taste import dispatch
    from app.infrastructure.memory.taste_profile import get_taste_store

    r = await dispatch(
        {
            "source": "free_text",
            "keyword_likes": ["minimal"],
            "keyword_dislikes": ["Neon"],
        },
        {"user_key": "u:7"},
    )
    assert r["ok"] is True and r["applied"] is True
    prof = get_taste_store().get_or_create("u:7")
    # L47/L49 branches executed (V2 reinforce semantics UNCHANGED — the
    # disliked-keyword reinforce applies the existing 0.9 cross-key decay, so
    # "minimal" lands at 0.9 not 1.0; we only assert the branch ran).
    assert prof.liked_keywords.get("minimal", 0) > 0  # L47 branch
    assert prof.disliked_keywords.get("neon", 0) > 0  # L49 branch
    # L61-63 — Gap4 flag-ON keyword-dislike ts record (stripped+lowercased).
    assert "neon" in prof.disliked_keywords_ts
    # The Gap4 ts loop's own strip-guard skips blank/whitespace keys.
    assert "" not in prof.disliked_keywords_ts


@pytest.mark.asyncio
async def test_update_taste_store_exception_fail_soft(monkeypatch):
    """update_taste L66-68 — store raising → store_failed:<ExcType>, not applied."""
    from app.agents.tools import update_taste as ut

    class _BoomStore:
        def get_or_create(self, k):
            raise RuntimeError("pg down")

    monkeypatch.setattr("app.infrastructure.memory.taste_profile.get_taste_store", lambda: _BoomStore())
    r = await ut.dispatch({"source": "free_text", "brand_dislikes": ["zara"]}, {"user_key": "u:7"})
    assert r["ok"] is False
    assert r["error"] == "store_failed:RuntimeError"
    assert r["applied"] is False


# ── BLOCKING-2 — apply_dislike_discount changed-code branches ──────────────


def test_apply_dislike_discount_no_user_key_returns_unchanged(monkeypatch):
    """search_products.apply_dislike_discount — flag ON but ctx has no
    user_key → returns the same list object (early return branch)."""
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools.search_products import apply_dislike_discount

    cands = [SimpleNamespace(brand="Gucci", name="x", title="x")]
    assert apply_dislike_discount({}, cands) is cands


def test_apply_dislike_discount_store_failure_fail_soft(monkeypatch):
    """search_products.apply_dislike_discount — store raising → cands unchanged
    (best-effort, never breaks search)."""
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools.search_products import apply_dislike_discount

    def _boom():
        raise RuntimeError("store down")

    monkeypatch.setattr("app.infrastructure.memory.taste_profile.get_taste_store", _boom)
    cands = [SimpleNamespace(brand="Gucci", name="x", title="x")]
    assert apply_dislike_discount({"user_key": "u:7"}, cands) is cands


def test_apply_dislike_discount_no_excludes_returns_unchanged(monkeypatch):
    """search_products.apply_dislike_discount — flag ON, profile has no
    recency-weighted excludes → unchanged."""
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools.search_products import apply_dislike_discount
    from app.infrastructure.memory.taste_profile import get_taste_store

    get_taste_store().get_or_create("u:7")  # clean profile, no dislikes
    cands = [SimpleNamespace(brand="Ami", name="y", title="y")]
    assert apply_dislike_discount({"user_key": "u:7"}, cands) is cands


def test_apply_dislike_discount_keyword_title_filter(monkeypatch):
    """search_products.apply_dislike_discount — recency keyword exclude drops a
    candidate whose title matches (the _keep title branch)."""
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools.search_products import apply_dislike_discount
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:7")
    prof.disliked_keywords_ts["neon"] = time.time()
    get_taste_store().update(prof)
    cands = [
        SimpleNamespace(brand="Ami", name="neon tote bag", title="neon tote bag"),
        SimpleNamespace(brand="Ami", name="black tote", title="black tote"),
    ]
    out = apply_dislike_discount({"user_key": "u:7"}, cands)
    assert [c.title for c in out] == ["black tote"]


@pytest.mark.asyncio
async def test_refine_search_dispatch_applies_dislike_discount(monkeypatch):
    """refine_search.dispatch (changed line) — real dispatch path runs
    run_text_only_search + apply_dislike_discount + persist."""
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools import refine_search
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:7")
    prof.disliked_brands_ts["gucci"] = time.time()
    get_taste_store().update(prof)

    async def _fake_text_search(**kw):
        return [
            SimpleNamespace(brand="Gucci", name="bag", title="bag", id="1"),
            SimpleNamespace(brand="Ami", name="tote", title="tote", id="2"),
        ]

    monkeypatch.setattr("app.agents.tools.refine_search.run_text_only_search", _fake_text_search)
    monkeypatch.setattr("app.agents.tools.refine_search.persist_last_results", lambda ctx, c: len(c))

    ctx = {"user_key": "u:7", "text_query": "bag", "chat_id": 7}
    r = await refine_search.dispatch({"action": "broaden"}, ctx)
    assert r["ok"] is True
    assert r["candidates_count"] == 1  # gucci discounted via Gap4 path


# ── BLOCKING-2 — _cap ts-overflow + search_products.dispatch real path ─────


def test_cap_truncates_ts_dicts_when_over_limit():
    """taste_profile._cap L153-157 — >50 ts entries trigger the additive
    ts-dict truncation (parity with the V2 score-dict cap)."""
    prof = TasteProfile(user_key="u:1")
    for i in range(60):
        prof.disliked_brands[f"b{i}"] = float(i)
        prof.disliked_brands_ts[f"b{i}"] = float(i)
        prof.disliked_keywords[f"k{i}"] = float(i)
        prof.disliked_keywords_ts[f"k{i}"] = float(i)
    prof._cap()
    assert len(prof.disliked_brands_ts) == 50  # capped
    assert len(prof.disliked_keywords_ts) == 50  # capped


@pytest.mark.asyncio
async def test_search_products_dispatch_runs_dislike_discount(monkeypatch):
    """search_products.dispatch (changed call site L316) — real dispatch path
    invokes apply_dislike_discount before persisting."""
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", True, raising=False)
    from app.agents.tools import search_products as sp
    from app.infrastructure.memory.taste_profile import get_taste_store

    prof = get_taste_store().get_or_create("u:7")
    prof.disliked_brands_ts["gucci"] = time.time()
    get_taste_store().update(prof)

    async def _fake_text_search(**kw):
        return [
            SimpleNamespace(brand="Gucci", name="bag", title="bag", id="1"),
            SimpleNamespace(brand="Ami", name="tote", title="tote", id="2"),
        ]

    monkeypatch.setattr("app.agents.tools.search_products.run_text_only_search", _fake_text_search)
    monkeypatch.setattr("app.agents.tools.search_products.persist_last_results", lambda ctx, c: len(c))

    ctx = {"user_key": "u:7", "chat_id": 7, "image_url": None}
    r = await sp.dispatch({"text_query": "bag"}, ctx)
    assert r["ok"] is True
    assert r["candidates_count"] == 1  # gucci discounted via the real dispatch path
