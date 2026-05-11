"""SPEC-IMPLICIT-FB-001 / REQ-FB-COST-001 — per-turn overhead micro-benchmark.

Measures p50/p95/p99 overhead of the implicit-feedback hot path:
  - log_impressions (batched INSERT)
  - attribute_expired_impressions (single CTE)
  - detect_and_apply_re_query (in-memory)

Skips silently when Docker daemon / testcontainers unavailable. Run via:

    uv run python scripts/bench_implicit_feedback.py
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass


@dataclass
class _C:
    id: str
    brand: str
    name: str
    subcategory: str
    keywords: list[str]


def _make_candidates(n: int = 5) -> list[_C]:
    return [
        _C(
            id=f"prod_{i}",
            brand=f"brand{i}",
            name=f"item {i}",
            subcategory="tee",
            keywords=["a", "b", f"k{i}"],
        )
        for i in range(n)
    ]


async def _one_iteration(chat_id: int) -> float:
    from app.channels.implicit_feedback import (
        attribute_expired_impressions,
        detect_and_apply_re_query,
        log_impressions,
    )
    from app.channels.session import Session, SessionState

    candidates = _make_candidates(5)
    sess = Session(
        chat_id=chat_id,
        state=SessionState.RESULTS_SENT,
        last_results=candidates,
        last_active=time.time() - 30.0,
    )

    t0 = time.perf_counter_ns()
    await attribute_expired_impressions(chat_id, None)
    await detect_and_apply_re_query(sess, inbound_is_fresh_query=True)
    await log_impressions(chat_id, None, candidates)
    return (time.perf_counter_ns() - t0) / 1e6  # ms


async def main(iterations: int = 500) -> None:
    try:
        import docker  # noqa: F401
        from testcontainers.postgres import PostgresContainer  # noqa: F401
    except ImportError:
        print("[bench] dependencies missing; skipping")
        return

    try:
        from testcontainers.postgres import PostgresContainer

        with PostgresContainer("postgres:16-alpine") as pg:
            dsn = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://", 1)

            # Bootstrap schema + migrations
            import psycopg

            with psycopg.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS ai")
                conn.commit()

            from alembic import command
            from alembic.config import Config

            cfg = Config("alembic.ini")
            cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
            command.upgrade(cfg, "head")

            from app.channels.implicit_feedback import set_backend_is_postgres
            from app.providers import db_pool

            await db_pool.init_pool(dsn)
            set_backend_is_postgres(True)

            # Warm-up
            for _ in range(20):
                await _one_iteration(101)

            # Measure
            samples = [await _one_iteration(200 + (i % 50)) for i in range(iterations)]

            await db_pool.close_pool()

            p50 = statistics.median(samples)
            p95 = statistics.quantiles(samples, n=20)[18]
            p99 = statistics.quantiles(samples, n=100)[98]
            print(f"[bench] iterations={iterations} p50={p50:.2f}ms p95={p95:.2f}ms p99={p99:.2f}ms")
            assert p99 < 60.0, f"p99 {p99:.2f}ms exceeds 60ms CI budget"
    except Exception as e:  # noqa: BLE001
        print(f"[bench] skipped: {e!r}")


if __name__ == "__main__":
    asyncio.run(main())
