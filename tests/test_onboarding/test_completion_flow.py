"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-COMPLETION-001.

`_onboard_helpers.complete_onboarding` single-seed-call discipline tests.

Covers:
  - Pinterest success path → exactly ONE seed call merging cards + pins
  - Pinterest skip path → exactly ONE seed call (card weights only)
  - Temporal ordering: seed call BEFORE sess.onboarded_at write
  - Sticky lang: KO session → KO completion text
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.graphs.nodes._onboard_helpers import complete_onboarding


# ────────────────────────────────────────────────────────────────────────────
# Fakes
# ────────────────────────────────────────────────────────────────────────────
@dataclass
class _FakeSession:
    chat_id: int = 1
    lang: str = "en"
    onboarded_at: datetime | None = None
    onboard_stage: str | None = None


@dataclass
class _FakeState:
    onboard_selections: dict = field(default_factory=lambda: {"mood": [], "color": [], "fit": []})
    onboard_pin_weights: dict | None = None
    onboard_stage: str | None = None


class _RecordingTasteStore:
    """Records seed_from_onboarding calls AND timestamps them.

    Timestamp lets `test_seed_before_mark_onboarded` assert ordering even
    when both events happen in the same microsecond budget.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.call_times: list[datetime] = []

    def seed_from_onboarding(self, user_key, weights):
        self.calls.append((user_key, dict(weights)))
        self.call_times.append(datetime.now(UTC))


class _RecordingSessionStore:
    def __init__(self):
        self.update_calls: list[_FakeSession] = []
        self.update_times: list[datetime] = []

    def update(self, sess):
        self.update_calls.append(sess)
        self.update_times.append(datetime.now(UTC))


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pinterest_success_makes_exactly_one_seed_call():
    """Card selections + Pinterest stash → ONE merged seed call."""
    state = _FakeState(
        onboard_selections={"mood": ["minimal"], "color": ["pastel"], "fit": ["oversize"]},
        onboard_pin_weights={
            "keyword_weights": {"hoodie": 0.1, "minimal": 0.1},  # `minimal` overlaps card
            "brand_weights": {"ami": 0.05},
            "successfully_analyzed": 2,
        },
    )
    sess = _FakeSession(lang="en")
    sess_store = _RecordingSessionStore()
    taste_store = _RecordingTasteStore()

    text, _ = await complete_onboarding(
        state,
        sess=sess,
        user_key="u:1",
        session_store=sess_store,
        taste_store=taste_store,
    )

    # CRITICAL: exactly one seed call.
    assert len(taste_store.calls) == 1
    user_key, weights = taste_store.calls[0]
    assert user_key == "u:1"
    # Card-only keys come through.
    assert "pastel" in weights
    assert "oversize" in weights
    # Overlap key sums card + pin contributions.
    # Card default weight = 0.5 (settings.ONBOARDING_CARD_SEED_WEIGHT); pin = 0.1.
    assert weights["minimal"] == pytest.approx(0.6)
    # Brand prefix.
    assert weights["brand:ami"] == pytest.approx(0.05)
    # Pinterest-success message variant.
    assert "Send a photo" in text or "Thanks" in text


@pytest.mark.asyncio
async def test_pinterest_skip_card_only_seed():
    """No Pinterest stash → ONE seed call with card weights only."""
    state = _FakeState(
        onboard_selections={"mood": ["minimal", "street"], "color": ["dark"], "fit": ["slim"]},
        onboard_pin_weights=None,
    )
    sess = _FakeSession(lang="en")
    taste_store = _RecordingTasteStore()

    text, _ = await complete_onboarding(
        state,
        sess=sess,
        user_key="u:2",
        session_store=_RecordingSessionStore(),
        taste_store=taste_store,
    )

    # Exactly ONE seed call.
    assert len(taste_store.calls) == 1
    _user_key, weights = taste_store.calls[0]
    # All 4 card values present.
    assert set(weights.keys()) == {"minimal", "street", "dark", "slim"}
    # No brand keys.
    assert not any(k.startswith("brand:") for k in weights)
    # Degraded message variant — Pinterest plug-in-later language.
    assert "Pinterest" in text or "taste" in text.lower()


@pytest.mark.asyncio
async def test_seed_before_mark_onboarded():
    """Temporal ordering — seed call MUST happen before sess.onboarded_at write."""
    state = _FakeState(
        onboard_selections={"mood": ["minimal"], "color": [], "fit": []},
    )
    sess = _FakeSession()
    sess_store = _RecordingSessionStore()
    taste_store = _RecordingTasteStore()

    await complete_onboarding(
        state,
        sess=sess,
        user_key="u:3",
        session_store=sess_store,
        taste_store=taste_store,
    )

    assert len(taste_store.call_times) == 1
    assert len(sess_store.update_times) == 1
    # Seed timestamp <= session.update timestamp (strictly before or simultaneous).
    assert taste_store.call_times[0] <= sess_store.update_times[0]


@pytest.mark.asyncio
async def test_completion_message_lang_sticky_ko():
    state = _FakeState(
        onboard_selections={"mood": ["minimal"], "color": [], "fit": []},
    )
    sess = _FakeSession(lang="ko")
    text, _ = await complete_onboarding(
        state,
        sess=sess,
        user_key="u:ko",
        session_store=_RecordingSessionStore(),
        taste_store=_RecordingTasteStore(),
    )
    # KO content marker — Korean script must appear.
    assert any("가" <= c <= "힣" for c in text)


@pytest.mark.asyncio
async def test_completion_message_lang_sticky_en():
    state = _FakeState(
        onboard_selections={"mood": ["minimal"], "color": [], "fit": []},
    )
    sess = _FakeSession(lang="en")
    text, _ = await complete_onboarding(
        state,
        sess=sess,
        user_key="u:en",
        session_store=_RecordingSessionStore(),
        taste_store=_RecordingTasteStore(),
    )
    # No Hangul in EN output.
    assert not any("가" <= c <= "힣" for c in text)


@pytest.mark.asyncio
async def test_clears_state_after_completion():
    state = _FakeState(
        onboard_selections={"mood": ["minimal"], "color": [], "fit": []},
        onboard_pin_weights={"keyword_weights": {"x": 0.1}, "brand_weights": {}, "successfully_analyzed": 1},
    )
    sess = _FakeSession()
    await complete_onboarding(
        state,
        sess=sess,
        user_key="u:clr",
        session_store=_RecordingSessionStore(),
        taste_store=_RecordingTasteStore(),
    )
    assert state.onboard_pin_weights is None
    assert state.onboard_stage == "done"
    assert sess.onboarded_at is not None


@pytest.mark.asyncio
async def test_seed_failure_aborts_onboarded_at():
    """Crash-safety — seed failure leaves onboarded_at NULL so retry path works."""

    class _BrokenTasteStore:
        def seed_from_onboarding(self, user_key, weights):
            raise RuntimeError("db down")

    state = _FakeState(onboard_selections={"mood": ["minimal"], "color": [], "fit": []})
    sess = _FakeSession()
    with pytest.raises(RuntimeError):
        await complete_onboarding(
            state,
            sess=sess,
            user_key="u:err",
            session_store=_RecordingSessionStore(),
            taste_store=_BrokenTasteStore(),
        )
    # onboarded_at NOT set — retry path can re-enter onboarding.
    assert sess.onboarded_at is None


@pytest.mark.asyncio
async def test_invalid_pin_weight_values_dropped():
    """Non-positive / non-numeric pin weights are dropped."""
    state = _FakeState(
        onboard_selections={"mood": ["minimal"], "color": [], "fit": []},
        onboard_pin_weights={
            "keyword_weights": {"a": 0.5, "b": -0.1, "c": 0.0, "d": "not-a-number"},
            "brand_weights": {"x": -1.0, "y": 0.1},
            "successfully_analyzed": 1,
        },
    )
    sess = _FakeSession()
    taste_store = _RecordingTasteStore()
    await complete_onboarding(
        state,
        sess=sess,
        user_key="u:filter",
        session_store=_RecordingSessionStore(),
        taste_store=taste_store,
    )
    _u, weights = taste_store.calls[0]
    assert "a" in weights
    assert "b" not in weights
    assert "c" not in weights
    assert "d" not in weights
    assert "brand:y" in weights
    assert "brand:x" not in weights


@pytest.mark.asyncio
async def test_unknown_card_values_ignored():
    """Stale persisted selection with bogus value should not poison the seed."""
    state = _FakeState(
        onboard_selections={"mood": ["minimal", "bogus_value"], "color": [], "fit": []},
    )
    sess = _FakeSession()
    taste_store = _RecordingTasteStore()
    await complete_onboarding(
        state,
        sess=sess,
        user_key="u:bogus",
        session_store=_RecordingSessionStore(),
        taste_store=taste_store,
    )
    _u, weights = taste_store.calls[0]
    assert "minimal" in weights
    assert "bogus_value" not in weights


@pytest.mark.asyncio
async def test_no_selections_no_pin_weights_skips_seed():
    """Edge case — no card selections AND no pin weights → no seed call.

    Onboarded_at is still marked so the user is treated as onboarded;
    avoiding the seed call is correct (nothing to seed). The follow-up
    /start path will gate via onboarded_at.
    """
    state = _FakeState()  # empty selections, None pin weights
    sess = _FakeSession()
    taste_store = _RecordingTasteStore()
    await complete_onboarding(
        state,
        sess=sess,
        user_key="u:none",
        session_store=_RecordingSessionStore(),
        taste_store=taste_store,
    )
    assert taste_store.calls == []
    assert sess.onboarded_at is not None
