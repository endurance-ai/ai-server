"""Unit tests for the brand_embedding_cache and brand_node_cache repositories.

SPEC-BRAND-2TOWER-RESCORE / SPEC-PERSONALIZE-RERANK. In-memory caches warmed
from a Postgres pool at lifespan startup. Tests cover the pure lookup/parse
helpers plus the fail-open warm paths (DB_DSN empty / pool not initialized) and
a successful warm via a mocked `run_in_pool_loop`.

Module-global `_cache` / `_warmed` are swapped to fresh values per test via
monkeypatch so warming never leaks across tests.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.infrastructure.repositories import brand_embedding_cache as bec
from app.infrastructure.repositories import brand_node_cache as bnc
from app.providers import db_pool


@pytest.fixture
def isolate_emb_cache(monkeypatch):
    monkeypatch.setattr(bec, "_cache", {})
    monkeypatch.setattr(bec, "_warmed", False)


@pytest.fixture
def isolate_node_cache(monkeypatch):
    monkeypatch.setattr(bnc, "_cache", {})
    monkeypatch.setattr(bnc, "_filter_index", {})
    monkeypatch.setattr(bnc, "_acronym_index", {})
    monkeypatch.setattr(bnc, "_warmed", False)


# ── brand_embedding_cache: lookup + parse ───────────────────────────────────


def test_emb_lookup_none_brand_id(isolate_emb_cache):
    assert bec.lookup(None) is None


def test_emb_lookup_empty_cache_miss(isolate_emb_cache):
    assert bec.lookup(1) is None
    assert bec.size() == 0


def test_emb_lookup_hit_after_manual_populate(isolate_emb_cache):
    bec._cache[7] = [0.1, 0.2]
    assert bec.lookup(7) == [0.1, 0.2]
    # string-coercible ids resolve.
    assert bec.lookup("7") == [0.1, 0.2]  # type: ignore[arg-type]
    assert bec.size() == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ([0.1, 0.2, 0.3], [0.1, 0.2, 0.3]),
        ("[0.1, 0.2, 0.3]", [0.1, 0.2, 0.3]),
        ("[]", []),
        (None, None),
        ("not-a-vector", None),
        ("[a, b]", None),  # unparseable floats
        ([1.0, "bad"], None),  # bad element in list
        (42, None),  # wrong type entirely
    ],
)
def test_parse_halfvec_shapes(raw, expected):
    assert bec._parse_halfvec(raw) == expected


# ── brand_embedding_cache: warm fail-open paths ─────────────────────────────


async def test_emb_warm_empty_dsn_keeps_empty(isolate_emb_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "")
    await bec.warm_cache()
    assert bec.is_warmed() is True
    assert bec.size() == 0


async def test_emb_warm_pool_none_keeps_empty(isolate_emb_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", None)
    await bec.warm_cache()
    assert bec.is_warmed() is True
    assert bec.size() == 0


async def test_emb_warm_success_filters_by_dim(isolate_emb_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", object())  # non-None sentinel

    good = "[" + ",".join("0.01" for _ in range(768)) + "]"
    rows = [
        (1, good),  # accepted
        (2, "[0.1, 0.2]"),  # wrong dim → skipped_dim
        (3, "garbage"),  # unparseable → skipped_parse
    ]

    def _fake_run(coro):
        coro.close()  # avoid "coroutine never awaited" warning
        return rows

    monkeypatch.setattr(db_pool, "run_in_pool_loop", _fake_run)
    await bec.warm_cache()
    assert bec.is_warmed() is True
    assert bec.size() == 1
    assert bec.lookup(1) is not None
    assert bec.lookup(2) is None


async def test_emb_warm_query_error_keeps_empty(isolate_emb_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", object())

    def _boom(coro):
        coro.close()
        raise RuntimeError("query failed")

    monkeypatch.setattr(db_pool, "run_in_pool_loop", _boom)
    await bec.warm_cache()
    assert bec.is_warmed() is True
    assert bec.size() == 0


# ── brand_node_cache: normalize + lookup + coerce ───────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1017 ALYX 9SM", "1017alyx9sm"),
        ("1017 Alyx 9SM", "1017alyx9sm"),
        ("Acne Studios", "acnestudios"),
        ("먼데이에디션", ""),  # Hangul is stripped by the Latin normalizer
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_brand(raw, expected):
    assert bnc.normalize_brand(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MONDAY EDITION (먼데이에디션)", "먼데이에디션"),
        ("먼데이 에디션", "먼데이에디션"),
        ("킨더살몬", "킨더살몬"),
        ("Acne Studios", ""),  # no Hangul → empty
        (None, ""),
        ("", ""),
    ],
)
def test_normalize_brand_ko(raw, expected):
    assert bnc.normalize_brand_ko(raw) == expected


@pytest.mark.parametrize(
    "brand_name,expected_keys",
    [
        # bilingual: outer Latin + inner Hangul both indexed
        ("MONDAY EDITION (먼데이에디션)", {"mondayedition", "먼데이에디션"}),
        # parenthetical acronym becomes its own Latin key; the whole-string
        # key ('postarchivefactionpaf') is also kept so a run-together query
        # like '포스트아카이브팩션(PAF)' still lands.
        ("Post Archive Faction (PAF)", {"postarchivefaction", "postarchivefactionpaf", "paf"}),
        # pure Korean
        ("킨더살몬", {"킨더살몬"}),
        # Korean-primary, English in parens
        ("오베르 (AUBER)", {"오베르", "auber"}),
    ],
)
def test_surface_keys(brand_name, expected_keys):
    assert bnc._surface_keys(brand_name) == expected_keys


@pytest.mark.parametrize(
    "latin_source,expected",
    [
        ("Post Archive Faction", "paf"),
        ("Comme des Garcons", "cdg"),
        ("Nike", ""),  # single word → no acronym
        ("", ""),
    ],
)
def test_acronym(latin_source, expected):
    assert bnc._acronym(latin_source) == expected


def test_node_lookup_none_and_empty(isolate_node_cache):
    assert bnc.lookup(None) is None
    assert bnc.lookup("") is None
    assert bnc.lookup("Unknown") is None


def test_node_lookup_hit_normalizes_key(isolate_node_cache):
    attrs = bnc.BrandAttributes(brand_name="Acme", brand_id=5)
    bnc._cache["acme"] = attrs
    # verbatim RPC brand string is normalized on lookup.
    assert bnc.lookup("Acme") is attrs
    assert bnc.lookup("ACME") is attrs
    assert bnc.size() == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ()),
        (["Bohemian", "Old-Money"], ("bohemian", "old-money")),
        ('["minimalist-architectural", "old-money"]', ("minimalist-architectural", "old-money")),
        ("[]", ()),
        ("", ()),
        (42, ()),  # wrong type
        (["", "ok"], ("ok",)),  # falsy element dropped
    ],
)
def test_coerce_vibe_shapes(raw, expected):
    assert bnc._coerce_vibe(raw) == expected


# ── brand_node_cache: warm fail-open + success ──────────────────────────────


async def test_node_warm_empty_dsn_keeps_empty(isolate_node_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "")
    await bnc.warm_cache()
    assert bnc.is_warmed() is True
    assert bnc.size() == 0


async def test_node_warm_pool_none_keeps_empty(isolate_node_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", None)
    await bnc.warm_cache()
    assert bnc.is_warmed() is True
    assert bnc.size() == 0


async def test_node_warm_success_builds_attrs(isolate_node_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", object())

    # Column order matches the SELECT in warm_cache:
    # brand_name, brand_name_normalized, primary_style_node_id, vibe,
    # silhouette, price_tier, formality, gender_lean, era_reference,
    # price_min_usd, price_max_usd, id
    rows = [
        (
            "Acme Studios",
            "acmestudios",
            3,
            ["minimal"],
            ["boxy"],
            "luxury",
            "casual",
            "womens",
            "y2k",
            100.0,
            500.0,
            9,
        ),
    ]

    def _fake_run(coro):
        coro.close()
        return rows

    monkeypatch.setattr(db_pool, "run_in_pool_loop", _fake_run)
    await bnc.warm_cache()
    assert bnc.is_warmed() is True
    attrs = bnc.lookup("Acme Studios")
    assert attrs is not None
    assert attrs.brand_id == 9
    assert attrs.primary_style_node_id == 3
    assert attrs.vibe == ("minimal",)
    assert attrs.silhouette == ("boxy",)
    assert attrs.gender_lean == "womens"
    assert attrs.price_min_usd == 100.0
    assert attrs.price_max_usd == 500.0


async def test_node_warm_query_error_keeps_empty(isolate_node_cache, monkeypatch):
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", object())

    def _boom(coro):
        coro.close()
        raise RuntimeError("query failed")

    monkeypatch.setattr(db_pool, "run_in_pool_loop", _boom)
    await bnc.warm_cache()
    assert bnc.is_warmed() is True
    assert bnc.size() == 0


# ── brand_node_cache: bilingual / acronym / duplicate-node resolution ────────


def _node_row(brand_name: str, normalized: str, node_id: int) -> tuple:
    """A brand_nodes row in the warm_cache SELECT column order (attrs empty)."""
    return (brand_name, normalized, None, None, None, None, None, None, None, None, None, node_id)


async def _warm_with_rows(monkeypatch, rows: list[tuple]) -> None:
    monkeypatch.setattr(settings, "DB_DSN", "postgresql://x")
    monkeypatch.setattr(db_pool, "_pool", object())

    def _fake_run(coro):
        coro.close()
        return rows

    monkeypatch.setattr(db_pool, "run_in_pool_loop", _fake_run)
    await bnc.warm_cache()


async def test_resolve_bilingual_and_acronym_and_duplicate_nodes(isolate_node_cache, monkeypatch):
    await _warm_with_rows(
        monkeypatch,
        [
            # duplicate nodes for the same real brand (products split across both)
            _node_row("Post Archive Faction", "post archive faction", 3922),
            _node_row("Post Archive Faction (PAF)", "post archive faction (paf)", 95),
            # bilingual node — English + Korean surfaces
            _node_row("MONDAY EDITION (먼데이에디션)", "mondayedition", 10),
            # pure Korean node
            _node_row("킨더살몬", "킨더살몬", 11),
        ],
    )

    # The '(PAF)' parenthetical AND the initials acronym both point at ONE group
    # → 'paf' resolves to BOTH canonical names so the RPC matches products under
    # either duplicate node.
    paf = bnc.resolve_brand_names("paf")
    assert paf == ["Post Archive Faction", "Post Archive Faction (PAF)"]
    # English and (transliterated) canonical both resolve to the same group.
    assert bnc.resolve_brand_names("post archive faction") == paf

    # Bilingual: either language of query resolves.
    assert bnc.resolve_brand_names("monday edition") == ["MONDAY EDITION (먼데이에디션)"]
    assert bnc.resolve_brand_names("먼데이에디션") == ["MONDAY EDITION (먼데이에디션)"]

    # Pure Korean brand resolves from a Korean query.
    assert bnc.resolve_brand_names("킨더살몬") == ["킨더살몬"]

    # Unknown → fail-open.
    assert bnc.resolve_brand_names("존재하지않는브랜드") is None
    assert bnc.resolve_brand_names(None) is None

    # rerank/diversity lookup path resolves the Korean surface too.
    assert bnc.lookup("먼데이에디션") is not None
    assert bnc.lookup("킨더살몬") is not None


async def test_ambiguous_acronym_is_dropped(isolate_node_cache, monkeypatch):
    # Two unrelated brands share the initials 'cdg' → the acronym is ambiguous
    # and must NOT resolve (never filter on the wrong brand).
    await _warm_with_rows(
        monkeypatch,
        [
            _node_row("Comme des Garcons", "comme des garcons", 1),
            _node_row("Casa Del Gato", "casa del gato", 2),
        ],
    )
    assert bnc.resolve_brand_names("cdg") is None
    # but the full names still resolve exactly.
    assert bnc.resolve_brand_names("comme des garcons") == ["Comme des Garcons"]
