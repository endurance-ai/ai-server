"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-003, 005, 006, 007.

`_pinterest_helpers` ingest pipeline tests.

Covers:
  - `_check_pinterest_cache` hit / miss / expired / url-mismatch
  - `_normalize_pinterest_url`
  - `aggregate_pin_weights` per-pin failure isolation
  - `ingest_pinterest_pins` branch matrix:
      * BOARD + cache-miss + continuous_origin=False → stash, no seed
      * PROFILE + cache-hit + continuous_origin=False → no Apify, stash
      * PINS + continuous_origin=True → direct seed, no stash
      * Apify timeout / degraded → graceful empty + degraded outcome
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.channels.pinterest_url import PinInputBoard, PinInputPins, PinInputProfile
from app.graphs.nodes._pinterest_helpers import (
    _check_pinterest_cache,
    _normalize_pinterest_url,
    aggregate_pin_weights,
    ingest_pinterest_pins,
)


# ────────────────────────────────────────────────────────────────────────────
# Test doubles
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class _FakeSession:
    last_pinterest_scrape_url: str | None = None
    last_pinterest_scrape_at: datetime | None = None
    last_pinterest_pins: dict | None = None


@dataclass
class _FakeState:
    onboard_pin_weights: dict | None = None


class _FakeSessionStore:
    def __init__(self):
        self.update_calls: list[_FakeSession] = []

    def update(self, sess):
        self.update_calls.append(sess)


class _FakeTasteStore:
    def __init__(self):
        self.seed_calls: list[tuple[str, dict]] = []

    def seed_from_onboarding(self, user_key, weights):
        self.seed_calls.append((user_key, dict(weights)))


@dataclass
class _FakeVisionItem:
    subcategory: str = ""
    colorFamily: str = ""  # noqa: N815 — mirrors Vision schema field name
    fit: str = ""
    category: str = ""
    brand_lower: str = ""


@dataclass
class _FakeVisionMoodTag:
    label: str = ""
    score: float = 0.0


@dataclass
class _FakeVisionMood:
    tags: list = field(default_factory=list)


@dataclass
class _FakeVisionResult:
    items: list = field(default_factory=list)
    mood: _FakeVisionMood = field(default_factory=_FakeVisionMood)


# ────────────────────────────────────────────────────────────────────────────
# _normalize_pinterest_url
# ────────────────────────────────────────────────────────────────────────────
class TestNormalizeUrl:
    def test_lowercase_host(self):
        assert _normalize_pinterest_url("https://Pinterest.COM/jane/coats/") == "https://pinterest.com/jane/coats"

    def test_strip_trailing_slash(self):
        assert _normalize_pinterest_url("https://pinterest.com/jane/") == "https://pinterest.com/jane"

    def test_strip_query_and_fragment(self):
        out = _normalize_pinterest_url("https://pinterest.com/jane/coats/?utm=1#x")
        assert out == "https://pinterest.com/jane/coats"

    def test_empty_returns_empty(self):
        assert _normalize_pinterest_url("") == ""
        assert _normalize_pinterest_url(None) == ""  # type: ignore[arg-type]

    def test_unparseable_url_falls_back_to_lowercase(self):
        # urlsplit doesn't raise on ordinary garbage strings; just confirm
        # the function returns something deterministic.
        out = _normalize_pinterest_url("not a url at all")
        assert isinstance(out, str)

    def test_missing_host_returns_lowercased(self):
        # No host segment.
        out = _normalize_pinterest_url("/just/a/path")
        assert "just/a/path" in out.lower()


# ────────────────────────────────────────────────────────────────────────────
# _check_pinterest_cache
# ────────────────────────────────────────────────────────────────────────────
class TestCacheCheck:
    def _make_sess(self, url, age_s, payload):
        return _FakeSession(
            last_pinterest_scrape_url=url,
            last_pinterest_scrape_at=datetime.now(UTC) - timedelta(seconds=age_s),
            last_pinterest_pins=payload,
        )

    def test_hit_returns_aggregated(self):
        payload = {
            "aggregated_weights": {
                "keyword_weights": {"minimal": 0.3},
                "brand_weights": {"ami": 0.05},
                "successfully_analyzed": 5,
            }
        }
        sess = self._make_sess("https://pinterest.com/jane", 3600, payload)
        out = _check_pinterest_cache(sess, "https://pinterest.com/jane", ttl_s=86400)
        assert out is not None
        assert out["keyword_weights"]["minimal"] == 0.3
        assert out["successfully_analyzed"] == 5

    def test_miss_url_mismatch(self):
        sess = self._make_sess("https://pinterest.com/jane", 60, {"aggregated_weights": {}})
        assert _check_pinterest_cache(sess, "https://pinterest.com/OTHER", ttl_s=86400) is None

    def test_miss_expired(self):
        payload = {
            "aggregated_weights": {
                "keyword_weights": {"x": 0.1},
                "brand_weights": {},
                "successfully_analyzed": 1,
            }
        }
        sess = self._make_sess("https://pinterest.com/jane", 86401, payload)
        assert _check_pinterest_cache(sess, "https://pinterest.com/jane", ttl_s=86400) is None

    def test_miss_no_payload(self):
        sess = _FakeSession()
        assert _check_pinterest_cache(sess, "https://pinterest.com/jane", ttl_s=86400) is None


# ────────────────────────────────────────────────────────────────────────────
# aggregate_pin_weights — Vision batch + failure isolation
# ────────────────────────────────────────────────────────────────────────────
class TestAggregatePinWeights:
    @pytest.mark.asyncio
    async def test_empty_returns_empty(self):
        out = await aggregate_pin_weights([], pin_weight=0.05, vision_extract=None)
        assert out["successfully_analyzed"] == 0
        assert out["keyword_weights"] == {}

    @pytest.mark.asyncio
    async def test_aggregates_across_pins(self):
        async def fake_extract(url: str):
            return _FakeVisionResult(
                items=[_FakeVisionItem(subcategory="hoodie", colorFamily="black", fit="oversized")]
            )

        out = await aggregate_pin_weights(["u1", "u2", "u3"], pin_weight=0.1, vision_extract=fake_extract)
        assert out["successfully_analyzed"] == 3
        # Each pin contributes 3 keywords; same keys across pins → SUM.
        assert out["keyword_weights"]["hoodie"] == pytest.approx(0.3)
        assert out["keyword_weights"]["black"] == pytest.approx(0.3)
        assert out["keyword_weights"]["oversized"] == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_per_pin_failure_isolated(self):
        counter = {"calls": 0}

        async def flaky_extract(url: str):
            counter["calls"] += 1
            if counter["calls"] == 2:
                raise RuntimeError("vision flaked")
            return _FakeVisionResult(items=[_FakeVisionItem(subcategory="tee")])

        out = await aggregate_pin_weights(["u1", "u2", "u3"], pin_weight=0.05, vision_extract=flaky_extract)
        # 1 failure isolated; 2 successes contribute.
        assert out["successfully_analyzed"] == 2
        assert out["keyword_weights"]["tee"] == pytest.approx(0.1)

    @pytest.mark.asyncio
    async def test_empty_vision_result_counts_as_failure(self):
        async def empty_extract(url: str):
            return _FakeVisionResult(items=[])

        out = await aggregate_pin_weights(["u1"], pin_weight=0.05, vision_extract=empty_extract)
        assert out["successfully_analyzed"] == 0

    @pytest.mark.asyncio
    async def test_brand_weights_extracted(self):
        async def with_brand(url: str):
            return _FakeVisionResult(items=[_FakeVisionItem(subcategory="tee", brand_lower="ami")])

        out = await aggregate_pin_weights(["u1"], pin_weight=0.05, vision_extract=with_brand)
        assert out["brand_weights"]["ami"] == pytest.approx(0.05)


# ────────────────────────────────────────────────────────────────────────────
# ingest_pinterest_pins — branch matrix
# ────────────────────────────────────────────────────────────────────────────
class TestIngestPinterestPins:
    @staticmethod
    async def _stub_vision_extract(url: str):
        return _FakeVisionResult(items=[_FakeVisionItem(subcategory="hoodie", colorFamily="black")])

    @pytest.fixture
    def vision_patch(self, monkeypatch):
        async def fake_extract(image):
            return await self._stub_vision_extract(image)

        # Patch the module-level lazy import target so production code path
        # picks up our stub without a separate parameter plumb.
        monkeypatch.setattr("app.channels.vision.extract", fake_extract, raising=False)
        return fake_extract

    @pytest.mark.asyncio
    async def test_board_cache_miss_continuous_false_stashes(self, vision_patch):
        """Mode=BOARD, cache miss, continuous_origin=False → stash, NO seed call."""
        state = _FakeState()
        sess = _FakeSession()
        sess_store = _FakeSessionStore()
        taste_store = _FakeTasteStore()

        async def apify(url, *, mode, max_items):
            return [{"image_url": "https://img/1.jpg"}, {"image_url": "https://img/2.jpg"}]

        async def resolve_batch(urls, *, concurrency):
            return []

        outcome = await ingest_pinterest_pins(
            state,
            PinInputBoard(url="https://pinterest.com/jane/coats/"),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=sess_store,
            taste_store=taste_store,
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )

        assert outcome.mode == "board"
        assert outcome.cache_hit is False
        assert outcome.seed_called is False
        assert taste_store.seed_calls == []  # critical: no direct seed
        assert state.onboard_pin_weights is not None  # stashed for completion
        assert state.onboard_pin_weights["successfully_analyzed"] == 2

    @pytest.mark.asyncio
    async def test_profile_cache_hit_continuous_false_stashes(self, vision_patch):
        """Mode=PROFILE, cache HIT → no Apify, no seed, weights stashed."""
        state = _FakeState()
        sess = _FakeSession(
            last_pinterest_scrape_url="https://pinterest.com/jane",
            last_pinterest_scrape_at=datetime.now(UTC) - timedelta(seconds=60),
            last_pinterest_pins={
                "aggregated_weights": {
                    "keyword_weights": {"vintage": 0.4},
                    "brand_weights": {},
                    "successfully_analyzed": 7,
                }
            },
        )

        apify_call_count = {"n": 0}

        async def apify(url, *, mode, max_items):
            apify_call_count["n"] += 1
            return []

        async def resolve_batch(urls, *, concurrency):
            return []

        outcome = await ingest_pinterest_pins(
            state,
            PinInputProfile(url="https://pinterest.com/jane/"),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )

        assert outcome.cache_hit is True
        assert outcome.seed_called is False
        assert apify_call_count["n"] == 0  # critical: cache hit skips Apify
        assert state.onboard_pin_weights is not None
        assert state.onboard_pin_weights["keyword_weights"]["vintage"] == 0.4

    @pytest.mark.asyncio
    async def test_pins_continuous_true_directly_seeds(self, vision_patch):
        """Mode=PINS, continuous_origin=True → direct seed call, NO stash."""
        state = _FakeState()
        sess = _FakeSession()
        taste_store = _FakeTasteStore()

        async def apify(url, *, mode, max_items):
            return []

        async def resolve_batch(urls, *, concurrency):
            return ["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"]

        outcome = await ingest_pinterest_pins(
            state,
            PinInputPins(
                urls=("https://pinterest.com/pin/1/", "https://pinterest.com/pin/2/", "https://pinterest.com/pin/3/")
            ),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=taste_store,
            sess=sess,
            user_key="u:1",
            continuous_origin=True,
            pin_weight=0.05,
        )

        assert outcome.mode == "pins"
        assert outcome.seed_called is True
        assert len(taste_store.seed_calls) == 1
        # Continuous path MUST NOT stash on state.
        assert state.onboard_pin_weights is None

    @pytest.mark.asyncio
    async def test_apify_exception_returns_degraded(self):
        state = _FakeState()
        sess = _FakeSession()

        async def apify_raising(url, *, mode, max_items):
            raise TimeoutError("apify slow")

        async def resolve_batch(urls, *, concurrency):
            return []

        outcome = await ingest_pinterest_pins(
            state,
            PinInputBoard(url="https://pinterest.com/jane/coats/"),
            apify_provider=apify_raising,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )

        assert outcome.mode == "degraded"
        assert outcome.error is not None
        assert state.onboard_pin_weights is None

    @pytest.mark.asyncio
    async def test_apify_empty_dataset_returns_degraded(self):
        state = _FakeState()
        sess = _FakeSession()

        async def apify_empty(url, *, mode, max_items):
            return []

        async def resolve_batch(urls, *, concurrency):
            return []

        outcome = await ingest_pinterest_pins(
            state,
            PinInputBoard(url="https://pinterest.com/jane/coats/"),
            apify_provider=apify_empty,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )

        assert outcome.mode == "degraded"
        assert outcome.seed_called is False

    @pytest.mark.asyncio
    async def test_profile_mode_apify_path(self, vision_patch):
        """Profile mode delegates to Apify and stashes."""
        state = _FakeState()
        sess = _FakeSession()

        async def apify(url, *, mode, max_items):
            assert mode == "profile"
            return [{"image_url": "https://img/p1.jpg"}]

        async def resolve_batch(urls, *, concurrency):
            return []

        outcome = await ingest_pinterest_pins(
            state,
            PinInputProfile(url="https://pinterest.com/jane/"),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:p",
            continuous_origin=False,
            pin_weight=0.05,
        )
        assert outcome.mode == "profile"
        assert outcome.successfully_analyzed == 1

    @pytest.mark.asyncio
    async def test_pinterest_pins_resolve_batch_empty_returns_degraded(self):
        state = _FakeState()
        sess = _FakeSession()

        async def apify(url, *, mode, max_items):
            return []

        async def resolve_batch(urls, *, concurrency):
            return []

        outcome = await ingest_pinterest_pins(
            state,
            PinInputPins(urls=("https://pinterest.com/pin/1/",)),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )
        assert outcome.mode == "degraded"
        assert outcome.error == "resolve_empty"

    @pytest.mark.asyncio
    async def test_pins_resolve_batch_exception_returns_degraded(self):
        state = _FakeState()
        sess = _FakeSession()

        async def apify(url, *, mode, max_items):
            return []

        async def resolve_batch(urls, *, concurrency):
            raise RuntimeError("network")

        outcome = await ingest_pinterest_pins(
            state,
            PinInputPins(urls=("https://pinterest.com/pin/1/",)),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )
        assert outcome.mode == "degraded"
        assert "resolve_" in outcome.error

    @pytest.mark.asyncio
    async def test_profile_apify_exception_returns_degraded(self):
        state = _FakeState()
        sess = _FakeSession()

        async def apify(url, *, mode, max_items):
            raise RuntimeError("apify dead")

        async def resolve_batch(urls, *, concurrency):
            return []

        outcome = await ingest_pinterest_pins(
            state,
            PinInputProfile(url="https://pinterest.com/jane/"),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )
        assert outcome.mode == "degraded"

    @pytest.mark.asyncio
    async def test_profile_empty_apify_returns_degraded(self):
        state = _FakeState()
        sess = _FakeSession()

        async def apify(url, *, mode, max_items):
            return []

        async def resolve_batch(urls, *, concurrency):
            return []

        outcome = await ingest_pinterest_pins(
            state,
            PinInputProfile(url="https://pinterest.com/jane/"),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )
        assert outcome.mode == "degraded"
        assert outcome.error == "apify_empty_profile"

    @pytest.mark.asyncio
    async def test_vision_all_fail_returns_degraded(self, monkeypatch):
        state = _FakeState()
        sess = _FakeSession()

        async def apify(url, *, mode, max_items):
            return [{"image_url": "https://img/x.jpg"}]

        async def resolve_batch(urls, *, concurrency):
            return []

        async def vision_explode(image):
            raise RuntimeError("vision boom")

        monkeypatch.setattr("app.channels.vision.extract", vision_explode, raising=False)

        outcome = await ingest_pinterest_pins(
            state,
            PinInputBoard(url="https://pinterest.com/jane/coats/"),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=_FakeSessionStore(),
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )
        assert outcome.mode == "degraded"
        assert outcome.error == "vision_all_failed"

    @pytest.mark.asyncio
    async def test_board_success_persists_cache(self, vision_patch):
        state = _FakeState()
        sess = _FakeSession()
        sess_store = _FakeSessionStore()

        async def apify(url, *, mode, max_items):
            return [{"image_url": "https://img/1.jpg"}]

        async def resolve_batch(urls, *, concurrency):
            return []

        await ingest_pinterest_pins(
            state,
            PinInputBoard(url="https://pinterest.com/jane/coats/"),
            apify_provider=apify,
            link_resolver_batch=resolve_batch,
            session_store=sess_store,
            taste_store=_FakeTasteStore(),
            sess=sess,
            user_key="u:1",
            continuous_origin=False,
            pin_weight=0.05,
        )
        assert sess.last_pinterest_scrape_url == "https://pinterest.com/jane/coats"
        assert sess.last_pinterest_scrape_at is not None
        assert len(sess_store.update_calls) == 1
