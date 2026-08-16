"""Standalone notification detector and APNs delivery worker.

Run as a separate container/process so web request latency and deployment
replicas do not own scheduling. Postgres advisory locks and SKIP LOCKED make
multiple worker replicas safe.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from datetime import time as clock_time
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

from app.core.config import settings
from app.services.notifications import (
    BRAND_JOB,
    SAVED_JOB,
    DeliveryReport,
    deliver_pending,
    run_notify_batch,
)
from app.services.push.apns import close_clients

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="kiko notification worker")
    parser.add_argument("--once", action="store_true", help="run one scheduling/delivery cycle and exit")
    parser.add_argument("--force", action="store_true", help="run both detectors regardless of today's job state")
    parser.add_argument("--dry-run", action="store_true", help="detect without writes or delivery")
    return parser.parse_args()


def _clock(value: str) -> clock_time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return clock_time(hour, minute)


async def _job_due(pool: AsyncConnectionPool, job: str, scheduled: str, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(settings.NOTIFY_TIMEZONE))
    if local.time() < _clock(scheduled):
        return False
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT last_succeeded_at FROM ai.notification_job_state WHERE job = %s",
            (job,),
        )
        row = await cur.fetchone()
    if not row or row[0] is None:
        return True
    return row[0].astimezone(local.tzinfo).date() < local.date()


async def _with_job_lock(pool: AsyncConnectionPool, job: str, operation) -> bool:
    async with pool.connection() as lock_conn, lock_conn.cursor() as cur:
        await cur.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (job,))
        locked = bool((await cur.fetchone())[0])
        if not locked:
            return False
        try:
            try:
                await operation()
            except Exception as exc:
                failed_at = datetime.now(tz=UTC)
                await cur.execute(
                    """
                    INSERT INTO ai.notification_job_state
                        (job, watermark, last_started_at, heartbeat_at, last_error)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (job) DO UPDATE SET
                        last_started_at = EXCLUDED.last_started_at,
                        heartbeat_at = EXCLUDED.heartbeat_at,
                        last_error = EXCLUDED.last_error,
                        updated_at = now()
                    """,
                    (job, failed_at, failed_at, failed_at, str(exc)[:2000]),
                )
                await lock_conn.commit()
                raise
            return True
        finally:
            await cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (job,))


async def _run_cycle(pool: AsyncConnectionPool, *, force: bool, dry_run: bool) -> None:
    now = datetime.now(tz=UTC)
    saved_due = force or await _job_due(pool, SAVED_JOB, settings.NOTIFY_SAVED_SCAN_TIME, now)
    brand_due = force or await _job_due(pool, BRAND_JOB, settings.NOTIFY_BRAND_SCAN_TIME, now)

    if saved_due:
        await _with_job_lock(
            pool,
            SAVED_JOB,
            lambda: run_notify_batch(
                pool,
                include_saved=True,
                include_brand=False,
                include_brand_sale=False,  # brand_sale 는 브랜드 스캔 사이클에서 하루 1회만.
                dry_run=dry_run,
                now=now,
            ),
        )
    if brand_due:
        await _with_job_lock(
            pool,
            BRAND_JOB,
            lambda: run_notify_batch(pool, include_saved=False, include_brand=True, dry_run=dry_run, now=now),
        )
    if not dry_run:
        report = await _drain_deliveries(pool, now=now)
        if report.claimed:
            logger.info(
                "🔔 [notify.worker] claimed=%d accepted=%d retry=%d failed=%d expired=%d",
                report.claimed,
                report.accepted,
                report.retried,
                report.failed,
                report.expired,
            )


async def _drain_deliveries(pool: AsyncConnectionPool, *, now: datetime) -> DeliveryReport:
    """아웃박스가 빌 때까지 클레임-발송을 반복한다. 사이클당 예산이 상한.

    사이클당 `deliver_pending` 을 한 번만 부르면 처리량이 배치 크기 ÷ 폴 간격으로 묶인다
    (기본 100건 / 30초 = 12,000건/시간). 다이제스트는 발송 시각에 한꺼번에 due 가 되는
    버스트라 이 상한이 곧 "마지막 유저가 몇 시간 뒤에 받는가" 가 된다.

    상한(NOTIFY_DELIVERY_MAX_PER_CYCLE)이 필요한 이유는 재시도다 — 즉시 재시도로 돌아오는
    딜리버리가 있으면 `claimed`0 이 영영 안 될 수 있고, 그러면 감지 스케줄 체크까지 굶는다.
    """
    total = DeliveryReport()
    budget = max(1, settings.NOTIFY_DELIVERY_MAX_PER_CYCLE)
    batch = max(1, settings.NOTIFY_DELIVERY_BATCH_SIZE)

    while total.claimed < budget:
        report = await deliver_pending(pool, now=now, limit=min(batch, budget - total.claimed))
        total.claimed += report.claimed
        total.accepted += report.accepted
        total.retried += report.retried
        total.failed += report.failed
        total.expired += report.expired
        total.endpoints_disabled += report.endpoints_disabled
        if not report.claimed:
            break
    else:
        logger.warning(
            "🔔 [notify.worker] cycle delivery budget exhausted at %d — 남은 딜리버리는 다음 사이클로",
            budget,
        )
    return total


async def _main(args: argparse.Namespace) -> int:
    if not settings.DB_DSN:
        logger.error("DB_DSN is required")
        return 2
    if not settings.NOTIFICATION_WORKER_ENABLED and not (args.force or args.dry_run):
        logger.error("NOTIFICATION_WORKER_ENABLED is false")
        return 2

    # _finish_delivery 가 딜리버리마다 커넥션을 잡으므로 풀이 APNs 동시성보다 작으면
    # 발송이 커넥션 대기로 직렬화된다. 여유분 2는 감지 쿼리·job_state 갱신 몫.
    max_size = max(4, settings.NOTIFY_APNS_CONCURRENCY + 2)
    try:
        async with AsyncConnectionPool(settings.DB_DSN, min_size=1, max_size=max_size, open=False) as pool:
            await pool.wait()
            while True:
                try:
                    await _run_cycle(pool, force=args.force, dry_run=args.dry_run)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("🔔 [notify.worker] cycle failed")
                if args.once or args.force or args.dry_run:
                    return 0
                await asyncio.sleep(max(5, settings.NOTIFY_WORKER_POLL_S))
    finally:
        # 클라이언트가 프로세스 수명이라 종료는 여기서만 일어난다 (apns.get_client 참조).
        await close_clients()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
    return asyncio.run(_main(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
