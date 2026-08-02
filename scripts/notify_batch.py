"""알림 배치 — 재입고 · 가격 하락 · 관심 브랜드 신규 상품.

세 감지기를 순차 실행하고 유저별 다이제스트와 기기별 delivery 를 outbox 에 적재한다.
실제 APNs 발송은 별도 `app.workers.notification_worker` 프로세스가 담당한다.
감지 로직과 발송 정책은 `app/services/notifications.py` 가 소유한다.

사용:
    # 발송 없이 대상만 집계 (첫 배포는 여기부터 — 볼륨과 임계치 실측)
    uv run python scripts/notify_batch.py --dry-run

    # outbox 적재 (발송은 worker 가 예정 시각에 수행)
    uv run python scripts/notify_batch.py

    # 특정 유저 대상으로 outbox 적재 검증
    uv run python scripts/notify_batch.py --only-user=<uuid> --limit=1

DSN 은 `DB_DSN` (app/core/config.py) 을 쓴다. `--dsn` 으로 덮어쓸 수 있다.
멱등: 저장상품 이벤트는 KST 일 단위, 브랜드 신상품은 유저/상품 평생 단위로
중복 제거되며 message/delivery 는 별도 transactional outbox 로 보존된다.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg_pool import AsyncConnectionPool  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.notifications import run_notify_batch  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="kiko notification batch")
    parser.add_argument("--dry-run", action="store_true", help="발송/적재 없이 대상만 집계")
    parser.add_argument("--only-user", type=UUID, default=None, help="특정 유저로 한정")
    parser.add_argument("--limit", type=int, default=None, help="푸시할 유저 수 상한")
    parser.add_argument("--dsn", default="", help="Postgres DSN (기본: settings.DB_DSN)")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    dsn = args.dsn or settings.DB_DSN
    if not dsn:
        print("DB_DSN is not set (use --dsn or export DB_DSN)", file=sys.stderr)
        return 2

    async with AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False) as pool:
        await pool.wait()
        report = await run_notify_batch(
            pool,
            dry_run=args.dry_run,
            only_user=args.only_user,
            limit=args.limit,
        )

    print(report.as_line())
    return 3 if report.aborted else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
