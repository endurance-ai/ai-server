"""알림 이력 보존 잡 — 기간이 지난 메시지·알림·끝난 브랜드 소식을 지운다.

지우는 이유는 용량이 아니라 **만료 스윕 비용**이다. `claim_due_deliveries` 는 워커 폴
간격(기본 30초)마다 돌고, `ai.notification_messages` 는 유저·카테고리·날짜당 1건씩 영구히
쌓인다 — 유저 1만이면 연 100만 행 규모다.

**되돌릴 수 없다.** 반드시 --dry-run 으로 규모를 먼저 확인한다:

    # 지울 대상만 센다 (아무것도 지우지 않음)
    uv run python scripts/notify_retention.py --dry-run

    # 실제 삭제 (NOTIFY_RETENTION_ENABLED 와 무관하게 수동 실행은 항상 동작)
    uv run python scripts/notify_retention.py

    # 보존 기간을 이번 실행에만 다르게
    uv run python scripts/notify_retention.py --dry-run --feed-days=365

워커에 자동 편성하려면 `NOTIFY_RETENTION_ENABLED=true` 로 켠다 (기본 false). 그러면
`NOTIFY_RETENTION_SCAN_TIME` 에 하루 1회 돈다. 보존 기간은 `NOTIFY_RETENTION_FEED_D`
(기본 180일) — 알림함 피드 쿼리에는 날짜 창이 없으므로 이 값이 사실상 노출 상한이다.

FK 캐스케이드가 부수 테이블을 함께 정리한다:
  notification_messages 삭제 → notification_deliveries, notification_message_events
  notifications 삭제        → notification_message_events
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg_pool import AsyncConnectionPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.notifications import purge_expired_notifications  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="kiko notification retention purge")
    parser.add_argument("--dry-run", action="store_true", help="지우지 않고 대상 행 수만 센다")
    parser.add_argument(
        "--feed-days",
        type=int,
        default=None,
        help=f"보존 기간(일). 기본: NOTIFY_RETENTION_FEED_D={settings.NOTIFY_RETENTION_FEED_D}",
    )
    parser.add_argument("--dsn", default="", help="Postgres DSN (기본: settings.DB_DSN)")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    dsn = args.dsn or settings.DB_DSN
    if not dsn:
        print("DB_DSN is not set (use --dsn or export DB_DSN)", file=sys.stderr)
        return 2
    if args.feed_days is not None:
        if args.feed_days < 1:
            print("--feed-days must be >= 1", file=sys.stderr)
            return 2
        settings.NOTIFY_RETENTION_FEED_D = args.feed_days

    async with AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False) as pool:
        await pool.wait()
        report = await purge_expired_notifications(pool, dry_run=args.dry_run)

    print(report.as_line())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
