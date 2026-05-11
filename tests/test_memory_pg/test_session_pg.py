"""REQ-MEMORY-SESSION-001/002 + REQ-MEMORY-PROTOCOL-002 (session delete)."""

from __future__ import annotations

import asyncio

import pytest

from app.channels.session import SessionState
from app.channels.session_pg import PostgresSessionStore, _to_jsonable


def test_get_or_create_creates_default_session():
    store = PostgresSessionStore()
    s = store.get_or_create(111)
    assert s.chat_id == 111
    assert s.state == SessionState.IDLE
    assert s.detected_items == []
    assert s.lang == "en"


def test_full_session_round_trip():
    """REQ-MEMORY-SESSION-001 — 23-field round-trip via JSONB."""
    store = PostgresSessionStore()
    s = store.get_or_create(222)
    s.state = SessionState.RESULTS_SENT
    s.from_user_id = 9001
    s.image_url = "https://example.com/x.jpg"
    s.detected_items = [{"label": "shirt", "score": 0.91}]
    s.selected_item_index = 0
    s.vision_keywords = ["oversized", "tee"]
    s.vision_item = "oversized tee"
    s.vision_result = {"styleNode": "minimal", "items": [{"subcategory": "tee"}]}
    s.vision_selected_item_index = 0
    s.vision_outfit_style_node_primary = "minimal"
    s.vision_outfit_style_node_secondary = "casual"
    s.vision_outfit_mood_tags = ["clean", "soft"]
    s.vision_outfit_gender = "unisex"
    s.user_intent = "find_similar"
    s.last_results = [{"product_id": "p1", "brand": "ami"}]
    s.shown_product_ids = ["p1", "p2"]
    s.last_critique_summary = "needs cheaper options"
    s.boost_keywords = ["oversized"]
    s.clarify_axis = "formality"
    s.clarify_value = "casual"
    s.lang = "ko"
    store.update(s)

    s2 = store.get_or_create(222)
    assert s2.state == SessionState.RESULTS_SENT
    assert s2.from_user_id == 9001
    assert s2.image_url == "https://example.com/x.jpg"
    assert s2.detected_items == [{"label": "shirt", "score": 0.91}]
    assert s2.vision_keywords == ["oversized", "tee"]
    assert s2.vision_result == {"styleNode": "minimal", "items": [{"subcategory": "tee"}]}
    assert s2.vision_outfit_mood_tags == ["clean", "soft"]
    assert s2.last_results == [{"product_id": "p1", "brand": "ami"}]
    assert s2.shown_product_ids == ["p1", "p2"]
    assert s2.boost_keywords == ["oversized"]
    assert s2.lang == "ko"


def test_lazy_expiry_returns_fresh_session():
    """REQ-MEMORY-SESSION-002 — expired row → fresh defaults on next read."""
    from app.providers.db_pool import get_pool, run_in_pool_loop

    store = PostgresSessionStore()
    s = store.get_or_create(333)
    s.state = SessionState.RESULTS_SENT
    s.boost_keywords = ["oversized"]
    store.update(s)

    # Manually set ttl_expires_at to 1s in the past
    async def _expire() -> None:
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE ai.user_session SET ttl_expires_at = now() - interval '1 second' WHERE chat_id = %s",
                (333,),
            )
            await conn.commit()

    run_in_pool_loop(_expire())

    s2 = store.get_or_create(333)
    assert s2.state == SessionState.IDLE
    assert s2.boost_keywords == []
    assert s2.last_results == []


def test_lazy_expiry_collapses_concurrent_reads_to_one_row():
    """REQ-MEMORY-SESSION-002 — 10 simultaneous expired reads ⇒ exactly one row."""
    from app.providers.db_pool import get_pool, run_in_pool_loop

    store = PostgresSessionStore()
    s = store.get_or_create(444)
    store.update(s)

    async def _expire() -> None:
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE ai.user_session SET ttl_expires_at = now() - interval '1 second' WHERE chat_id = %s",
                (444,),
            )
            await conn.commit()

    run_in_pool_loop(_expire())

    # Fire 10 concurrent get_or_create via threads (sync surface)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda _: store.get_or_create(444), range(10)))
    assert all(r.chat_id == 444 for r in results)

    async def _count() -> int:
        async with get_pool().connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM ai.user_session WHERE chat_id = %s", (444,))
            row = await cur.fetchone()
        return int(row[0])

    n = run_in_pool_loop(_count())
    assert n == 1


def test_delete_idempotent():
    """REQ-MEMORY-PROTOCOL-002."""
    store = PostgresSessionStore()
    store.delete(9999)  # missing — must not raise
    s = store.get_or_create(555)
    store.update(s)
    store.delete(555)
    store.delete(555)  # second delete silent


def test_to_jsonable_handles_pydantic_dataclass_and_primitives():
    """REQ-MEMORY-SESSION-001 — 5-step cascade."""
    from dataclasses import dataclass

    @dataclass
    class Foo:
        x: int

    assert _to_jsonable(None) is None
    assert _to_jsonable(42) == 42
    assert _to_jsonable("s") == "s"
    assert _to_jsonable([1, "a", None]) == [1, "a", None]
    assert _to_jsonable({"a": 1}) == {"a": 1}
    assert _to_jsonable(Foo(x=7)) == {"x": 7}


@pytest.mark.asyncio
async def test_start_stop_cleanup_task():
    store = PostgresSessionStore()
    await store.start()
    assert store._cleanup_task is not None
    await store.stop()
    assert store._cleanup_task is None


def test_lock_for_returns_asyncio_lock():
    store = PostgresSessionStore()
    lock = store.lock_for(1)
    assert isinstance(lock, asyncio.Lock)
    assert store.lock_for(1) is lock
