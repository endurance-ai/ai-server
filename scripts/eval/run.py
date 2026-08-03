"""V3 ReAct 플로우 검증 자동 시나리오 러너.

로컬 서버 (:8001) webhook 으로 Telegram Update 합성 POST + PG/Redis 관측.
출력: /tmp/v3_eval_results.json (시나리오별 dict 모음).

사용:
    uv run python scripts/eval/run.py [scenario_id]
        scenario_id 생략 시 전체 12개 실행.

NOTE: chat_id=999999999 (테스트 전용). Telegram API 호출은 "chat not found" 로 실패하나
내부 그래프/PG/Redis/Langfuse 흐름은 정상 동작 (어댑터 호출 결과 ok=False 만 차이).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 프로젝트 루트를 import path 에 추가
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import psycopg  # noqa: E402
import redis.asyncio as aioredis  # noqa: E402

from app.core.config import settings  # noqa: E402

WEBHOOK_URL = "http://localhost:8001/webhooks/telegram"
CHAT_ID = 999999999
USER_ID = 999999999
USERNAME = "kiko_eval_bot"
SECRET = settings.TELEGRAM_WEBHOOK_SECRET
DB_DSN = settings.DB_DSN
REDIS_URL = "redis://localhost:6379/1"

RESULTS_PATH = Path("/tmp/v3_eval_results.json")
LOGS_PATH = Path("/tmp/v3_eval_run.log")

# 핀터/IG 링크 (사용자 제공)
PINTEREST_URL = "https://pin.it/6W7EzMZdT"
IG_URL = "https://www.instagram.com/p/DTdKnfvE58w/"

# 시나리오 사이 대기 (background task 완료 + LLM/검색 latency)
WAIT_AFTER_TEXT = 25
WAIT_AFTER_IMAGE = 35
WAIT_AFTER_CALLBACK = 18


def log(msg: str) -> None:
    line = f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOGS_PATH.open("a") as f:
        f.write(line + "\n")


# ─────────────────────────────────────────── Telegram Update 합성

_msg_seq = 0
_update_seq = 0


def _next_message_id() -> int:
    global _msg_seq
    _msg_seq += 1
    return 80000000 + _msg_seq


def _next_update_id() -> int:
    global _update_seq
    _update_seq += 1
    return 90000000 + _update_seq


def _now_ts() -> int:
    return int(time.time())


def build_text_update(text: str) -> dict[str, Any]:
    return {
        "update_id": _next_update_id(),
        "message": {
            "message_id": _next_message_id(),
            "from": {"id": USER_ID, "is_bot": False, "username": USERNAME, "first_name": "KikoEval"},
            "chat": {"id": CHAT_ID, "type": "private", "username": USERNAME},
            "date": _now_ts(),
            "text": text,
        },
    }


def build_url_update(url: str) -> dict[str, Any]:
    """URL 을 entity 로 포함한 텍스트 메시지. parse_inbound 가 entities 에서 url 추출."""
    text = url
    return {
        "update_id": _next_update_id(),
        "message": {
            "message_id": _next_message_id(),
            "from": {"id": USER_ID, "is_bot": False, "username": USERNAME, "first_name": "KikoEval"},
            "chat": {"id": CHAT_ID, "type": "private", "username": USERNAME},
            "date": _now_ts(),
            "text": text,
            "entities": [{"type": "url", "offset": 0, "length": len(text)}],
        },
    }


def build_callback_update(callback_data: str, source_message_id: int | None = None) -> dict[str, Any]:
    msg = {
        "message_id": source_message_id or _next_message_id(),
        "chat": {"id": CHAT_ID, "type": "private", "username": USERNAME},
        "date": _now_ts(),
        "text": "(card)",
    }
    return {
        "update_id": _next_update_id(),
        "callback_query": {
            "id": str(_next_update_id() + 1000000),
            "from": {"id": USER_ID, "is_bot": False, "username": USERNAME, "first_name": "KikoEval"},
            "message": msg,
            "data": callback_data,
            "chat_instance": "test-chat-instance",
        },
    }


# ─────────────────────────────────────────── HTTP / Webhook 호출


async def post_update(client: httpx.AsyncClient, update: dict[str, Any]) -> int:
    resp = await client.post(
        WEBHOOK_URL,
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        timeout=10.0,
    )
    return resp.status_code


# ─────────────────────────────────────────── 관측 헬퍼


async def get_recent_events(after_ts: datetime, limit: int = 100) -> list[dict[str, Any]]:
    """이 chat_id 의 after_ts 이후 conversation_event 전체."""
    rows: list[dict[str, Any]] = []
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, thread_id, turn_no, event_type, payload, langfuse_trace, created_at
            FROM ai.log_conversation_event
            WHERE chat_id = %s AND created_at >= %s
            ORDER BY created_at ASC
            """,
            (CHAT_ID, after_ts),
        )
        for r in cur.fetchall():
            rows.append(
                {
                    "id": r[0],
                    "thread_id": str(r[1]),
                    "turn_no": r[2],
                    "event_type": r[3],
                    "payload": r[4],
                    "langfuse_trace": r[5],
                    "created_at": r[6].isoformat() if r[6] else None,
                }
            )
    return rows


async def get_session() -> dict[str, Any] | None:
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT state, lang, onboarded_at, last_results, boost_keywords
            FROM ai.user_session WHERE chat_id = %s
            """,
            (CHAT_ID,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "state": r[0],
            "lang": r[1],
            "onboarded_at": r[2].isoformat() if r[2] else None,
            "last_results_count": len(r[3] or []) if r[3] else 0,
            "boost_keywords": r[4],
        }


async def get_taste_profile() -> dict[str, Any] | None:
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_key, liked_brands, disliked_brands, liked_keywords, disliked_keywords, updated_at
            FROM ai.user_taste_profile WHERE user_key LIKE %s
            """,
            (f"u:{USER_ID}:%",),
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "user_key": r[0],
            "liked_brands": r[1],
            "disliked_brands": r[2],
            "liked_keywords": r[3],
            "disliked_keywords": r[4],
            "updated_at": r[5].isoformat() if r[5] else None,
        }


async def get_impressions(after_ts: datetime) -> list[dict[str, Any]]:
    rows = []
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT product_id, brand, shown_at, langfuse_trace
            FROM ai.card_impression
            WHERE chat_id = %s AND shown_at >= %s
            ORDER BY shown_at ASC
            """,
            (CHAT_ID, after_ts),
        )
        for r in cur.fetchall():
            rows.append({"product_id": r[0], "brand": r[1], "shown_at": r[2].isoformat(), "langfuse_trace": r[3]})
    return rows


async def get_redis_state() -> dict[str, Any]:
    # Resilient: when Redis is intentionally down (S12), probing must not crash
    # the whole scenario — return a sentinel instead.
    try:
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:  # noqa: BLE001
        return {"unreachable": True, "err": type(e).__name__}
    try:
        cursor = await r.get(f"kiko:cursor:{CHAT_ID}")
        imp = await r.smembers(f"kiko:imp:{CHAT_ID}")
        cursor_ttl = await r.ttl(f"kiko:cursor:{CHAT_ID}")
        imp_ttl = await r.ttl(f"kiko:imp:{CHAT_ID}")
        return {
            "cursor": int(cursor) if cursor else None,
            "cursor_ttl_s": cursor_ttl,
            "impressions_count": len(imp),
            "imp_ttl_s": imp_ttl,
        }
    except Exception as e:  # noqa: BLE001 — Redis down (S12) → sentinel, never crash
        return {"unreachable": True, "err": type(e).__name__}
    finally:
        try:
            await r.aclose()
        except Exception:  # noqa: BLE001
            pass


def _evt_types(events: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in events:
        out[e["event_type"]] = out.get(e["event_type"], 0) + 1
    return out


# ─────────────────────────────────────────── 시나리오 실행기


async def reset_chat_state() -> None:
    """테스트 전 chat_id 의 모든 데이터 제거."""
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM ai.log_conversation_event WHERE chat_id = %s", (CHAT_ID,))
        cur.execute("DELETE FROM ai.card_impression WHERE chat_id = %s", (CHAT_ID,))
        cur.execute("DELETE FROM ai.user_session WHERE chat_id = %s", (CHAT_ID,))
        cur.execute("DELETE FROM ai.user_taste_profile WHERE user_key LIKE %s", (f"u:{USER_ID}:%",))
        conn.commit()
    try:
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        await r.delete(f"kiko:cursor:{CHAT_ID}")
        await r.delete(f"kiko:imp:{CHAT_ID}")
        await r.aclose()
    except Exception as e:  # noqa: BLE001 — Redis may be down (S12); PG reset already done
        log(f"  (reset_chat_state: redis skip — {type(e).__name__})")


async def reset_onboarded_only() -> None:
    """onboarded_at 만 NULL — 신규 유저 시뮬레이션 (S1)."""
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute("UPDATE ai.user_session SET onboarded_at = NULL WHERE chat_id = %s", (CHAT_ID,))
        conn.commit()


async def run_scenario(
    sid: str,
    title: str,
    setup,
    actions: list[tuple[str, dict[str, Any], int]],
) -> dict[str, Any]:
    """
    setup: async callable (옵셔널 사전 정리)
    actions: list of (action_name, telegram_update, wait_seconds)
    """
    log(f"┌── {sid}: {title}")
    if setup is not None:
        await setup()

    started_at = datetime.now(UTC)
    async with httpx.AsyncClient() as client:
        for name, upd, wait_s in actions:
            log(f"│  → {name}")
            status = await post_update(client, upd)
            log(f"│    webhook status={status}")
            await asyncio.sleep(wait_s)

    events = await get_recent_events(started_at)
    session = await get_session()
    taste = await get_taste_profile()
    impressions = await get_impressions(started_at)
    redis_state = await get_redis_state()

    result = {
        "scenario_id": sid,
        "title": title,
        "started_at": started_at.isoformat(),
        "events_count": len(events),
        "event_types": _evt_types(events),
        "events": events[:80],  # 너무 길어지면 상한
        "session": session,
        "taste": taste,
        "impressions_count": len(impressions),
        "impressions": impressions[:20],
        "redis": redis_state,
    }
    log(f"└── {sid} done: events={len(events)} types={result['event_types']}")
    return result


# ─────────────────────────────────────────── 시나리오 정의


async def s1():
    return await run_scenario(
        "S1",
        "/start only — first-touch (신규 유저)",
        setup=reset_chat_state,
        actions=[("send /start", build_text_update("/start"), WAIT_AFTER_TEXT)],
    )


async def s2():
    return await run_scenario(
        "S2",
        "Pure text query (KO) — agent + search_products happy path",
        setup=None,  # 누적 상태 OK (S1 이후)
        actions=[
            (
                "send text",
                build_text_update("검정 오버사이즈 후드 추천해줘"),
                WAIT_AFTER_TEXT,
            )
        ],
    )


async def s3():
    """이미지 업로드 변형 — 공개 패션 사진 URL 텍스트로 전송. resolve_image → vision 경로 검증."""
    sample_url = "https://images.unsplash.com/photo-1542272604-787c3835535d?w=800"
    return await run_scenario(
        "S3",
        "Image URL — Vision happy path (Unsplash 패션 샷)",
        setup=None,
        actions=[("send image URL", build_url_update(sample_url), WAIT_AFTER_IMAGE)],
    )


async def s4():
    """Weak vision → clarify cards. 풀바디 멀티 아이템 사진으로 유도."""
    sample_url = "https://images.unsplash.com/photo-1496747611176-843222e1e57c?w=800"
    return await run_scenario(
        "S4",
        "Weak vision → clarify cards (멀티 아이템 풀샷)",
        setup=None,
        actions=[("send multi-item URL", build_url_update(sample_url), WAIT_AFTER_IMAGE)],
    )


async def s5():
    """Refine — S2 직후 가정. cards:refine callback."""
    return await run_scenario(
        "S5",
        "Refine (cards:refine 버튼)",
        setup=None,
        actions=[("send cards:refine", build_callback_update("cards:refine"), WAIT_AFTER_CALLBACK)],
    )


async def s6():
    """Pager — cards:more callback."""
    return await run_scenario(
        "S6",
        "Pager (cards:more)",
        setup=None,
        actions=[("send cards:more", build_callback_update("cards:more"), WAIT_AFTER_CALLBACK)],
    )


async def s7():
    """Like — card:like:{pid} callback (S2의 last_results 첫 product 사용)."""
    # last_results 에서 첫 product id 가져오기
    session = await get_session()
    pid_suffix = "0"
    if session:
        with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_results->0->>'id' FROM ai.user_session WHERE chat_id = %s",
                (CHAT_ID,),
            )
            r = cur.fetchone()
            if r and r[0]:
                pid_suffix = str(r[0])[-53:]  # _LIKE_SUFFIX_BUDGET
    cb = f"card:like:{pid_suffix}"
    return await run_scenario(
        "S7",
        f"Like → taste profile ({cb[:40]}...)",
        setup=None,
        actions=[
            ("send card:like", build_callback_update(cb), WAIT_AFTER_CALLBACK),
            ("follow-up text", build_text_update("비슷한 스타일로 더 추천해줘"), WAIT_AFTER_TEXT),
        ],
    )


async def s8():
    """EN sticky — 영어 시작 + 콜백 후에도 영어 유지."""
    await reset_chat_state()  # 깨끗한 상태에서 EN 진입
    return await run_scenario(
        "S8",
        "EN sticky — 영어 시작 + callback 후에도 영어",
        setup=None,
        actions=[
            ("send EN text", build_text_update("recommend me a cozy beige knit"), WAIT_AFTER_TEXT),
            ("send cards:refine (no text)", build_callback_update("cards:refine"), WAIT_AFTER_CALLBACK),
        ],
    )


async def s9():
    """Empty result → Reflexion fastpath. 말도 안 되는 조합."""
    return await run_scenario(
        "S9",
        "Empty result → Reflexion (Gap2) fastpath",
        setup=reset_chat_state,
        actions=[
            (
                "send absurd query",
                build_text_update("라벤더색 가죽 칠부 카프리 9XL 사이즈로 추천"),
                WAIT_AFTER_TEXT,
            )
        ],
    )


async def s10():
    """Pinterest 링크 (사용자 제공)."""
    return await run_scenario(
        "S10",
        "Pinterest link (사용자 제공)",
        setup=reset_chat_state,
        actions=[("send Pinterest URL", build_url_update(PINTEREST_URL), WAIT_AFTER_IMAGE)],
    )


async def s11():
    """IG 포스트 (사용자 제공)."""
    return await run_scenario(
        "S11",
        "Instagram post link (사용자 제공)",
        setup=reset_chat_state,
        actions=[("send IG URL", build_url_update(IG_URL), WAIT_AFTER_IMAGE + 10)],  # apify 좀 더 줌
    )


async def s12():
    """Redis down → fail-open. 로컬 docker redis 사용 가정."""
    log("S12: Redis 컨테이너 확인")
    redis_container = None
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for name in out.stdout.strip().split("\n"):
            if "redis" in name.lower():
                redis_container = name
                break
    except Exception as e:
        log(f"  docker ps 실패: {e}")

    if not redis_container:
        log("  Redis 컨테이너 없음 — S12 SKIP")
        return {
            "scenario_id": "S12",
            "title": "Redis down → fail-open",
            "skipped": True,
            "reason": "docker redis container not found locally",
        }

    log(f"  redis container = {redis_container}, stopping...")
    subprocess.run(["docker", "stop", redis_container], capture_output=True, timeout=10)
    await asyncio.sleep(2)

    try:
        result = await run_scenario(
            "S12",
            "Redis down → fail-open",
            setup=reset_chat_state,
            actions=[
                ("send text (Redis down)", build_text_update("화이트 셔츠 추천해줘"), WAIT_AFTER_TEXT),
            ],
        )
    finally:
        log(f"  restarting {redis_container}")
        subprocess.run(["docker", "start", redis_container], capture_output=True, timeout=10)
        await asyncio.sleep(3)

    return result


SCENARIOS = {
    "S1": s1,
    "S2": s2,
    "S3": s3,
    "S4": s4,
    "S5": s5,
    "S6": s6,
    "S7": s7,
    "S8": s8,
    "S9": s9,
    "S10": s10,
    "S11": s11,
    "S12": s12,
}


async def main() -> None:
    LOGS_PATH.unlink(missing_ok=True)
    log("V3 eval 시작")
    log(f"  CHAT_ID={CHAT_ID} USER_ID={USER_ID}")
    log(f"  DB={DB_DSN.split('@')[1].split('?')[0]}")
    log(f"  Webhook secret len={len(SECRET)}")

    only = sys.argv[1:] if len(sys.argv) > 1 else None
    targets = only or list(SCENARIOS.keys())
    log(f"  실행 대상: {targets}")

    results = []
    for sid in targets:
        if sid not in SCENARIOS:
            log(f"  ⚠️  unknown {sid}, skip")
            continue
        try:
            res = await SCENARIOS[sid]()
        except Exception as e:
            log(f"  ❌ {sid} raised: {type(e).__name__}: {e}")
            res = {"scenario_id": sid, "error": f"{type(e).__name__}: {e}"}
        results.append(res)
        # 시나리오 사이 쿨다운
        await asyncio.sleep(3)

    # 누적 — append/merge with existing
    existing: list[dict[str, Any]] = []
    if RESULTS_PATH.exists():
        with contextlib.suppress(Exception):
            existing = json.loads(RESULTS_PATH.read_text())
    by_id = {r.get("scenario_id"): r for r in existing}
    for r in results:
        by_id[r.get("scenario_id")] = r
    merged = list(by_id.values())
    RESULTS_PATH.write_text(json.dumps(merged, indent=2, default=str))
    log(f"결과 저장 → {RESULTS_PATH} (총 {len(merged)})")


if __name__ == "__main__":
    asyncio.run(main())
