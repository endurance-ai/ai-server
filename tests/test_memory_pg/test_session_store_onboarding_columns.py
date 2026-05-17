"""SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-MIGRATION-002 + REQ-ONBOARD-GRAPH-002.

PostgresSessionStore round-trip coverage for the 7 onboarding columns added
by migration 0004 (ONB-T11 state extension).

Verifies:
  - Empty/None defaults round-trip cleanly (backward compat for pre-onboarding rows)
  - Populated values (datetime + nested dicts) survive JSONB serialization
  - `onboarded_at` survives session reset (it is NOT cleared by TTL expiry)

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.infrastructure.memory.session_pg import PostgresSessionStore


def test_round_trip_empty_onboarding_columns():
    """Default-constructed Session has None / empty for all 7 onboarding fields."""
    store = PostgresSessionStore()
    s = store.get_or_create(900001)
    # Defaults per Session dataclass.
    assert s.onboarded_at is None
    assert s.onboard_stage is None
    assert s.onboard_selections == {}
    assert s.onboard_card_message_id is None
    assert s.last_pinterest_scrape_url is None
    assert s.last_pinterest_scrape_at is None
    assert s.last_pinterest_pins is None

    # Round-trip a no-op update — values remain None / empty.
    store.update(s)
    s2 = store.get_or_create(900001)
    assert s2.onboarded_at is None
    assert s2.onboard_stage is None
    assert s2.onboard_selections == {}
    assert s2.onboard_card_message_id is None
    assert s2.last_pinterest_scrape_url is None
    assert s2.last_pinterest_scrape_at is None
    assert s2.last_pinterest_pins is None


def test_round_trip_populated_onboarding_columns():
    """All 7 fields round-trip via JSONB / timestamptz."""
    store = PostgresSessionStore()
    s = store.get_or_create(900002)
    stamp = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    s.onboarded_at = stamp
    s.onboard_stage = "pinterest"
    s.onboard_selections = {
        "mood": ["minimal", "cleangirl"],
        "color": ["pastel"],
        "fit": ["oversize"],
    }
    s.onboard_card_message_id = 4242
    s.last_pinterest_scrape_url = "https://pinterest.com/jane/coats"
    s.last_pinterest_scrape_at = stamp
    s.last_pinterest_pins = {
        "image_urls": ["https://i.pinimg.com/a.jpg", "https://i.pinimg.com/b.jpg"],
        "aggregated_weights": {
            "keyword_weights": {"minimal": 0.5, "oversize": 0.3},
            "brand_weights": {"lemaire": 0.4},
            "successfully_analyzed": 9,
        },
    }
    store.update(s)

    s2 = store.get_or_create(900002)
    assert s2.onboarded_at is not None
    # Timestamp round-trip via timestamptz preserves wall-time at UTC.
    assert s2.onboarded_at.year == 2026 and s2.onboarded_at.month == 5
    assert s2.onboard_stage == "pinterest"
    assert s2.onboard_selections == {
        "mood": ["minimal", "cleangirl"],
        "color": ["pastel"],
        "fit": ["oversize"],
    }
    assert s2.onboard_card_message_id == 4242
    assert s2.last_pinterest_scrape_url == "https://pinterest.com/jane/coats"
    assert s2.last_pinterest_scrape_at is not None
    assert isinstance(s2.last_pinterest_pins, dict)
    assert s2.last_pinterest_pins["aggregated_weights"]["successfully_analyzed"] == 9
    assert s2.last_pinterest_pins["aggregated_weights"]["keyword_weights"]["minimal"] == pytest.approx(0.5)


def test_onboarded_at_survives_session_ttl_reset():
    """REQ-ONBOARD-MIGRATION-002 — `onboarded_at` is a permanent user marker.

    A session-TTL-expiry reset MUST NOT clear `onboarded_at`. The reset clause
    in `_aget_or_create` resets state/cards but leaves the onboarding mark
    intact so returning users do NOT re-onboard.
    """
    from app.providers.db_pool import get_pool, run_in_pool_loop

    store = PostgresSessionStore()
    s = store.get_or_create(900003)
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    s.onboarded_at = stamp
    s.onboard_stage = "done"
    store.update(s)

    async def _expire() -> None:
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE ai.user_session SET ttl_expires_at = now() - interval '1 second' WHERE chat_id = %s",
                (900003,),
            )
            await conn.commit()

    run_in_pool_loop(_expire())

    s2 = store.get_or_create(900003)
    # TTL reset re-IDLEs the session BUT keeps the onboarded_at marker so
    # the user does NOT re-enter onboarding on next /start.
    assert s2.onboarded_at is not None
    assert s2.onboarded_at.year == 2026
