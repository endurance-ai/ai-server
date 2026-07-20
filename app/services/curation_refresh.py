"""메인 큐레이션 구좌 백그라운드 refresher.

lifespan에서 asyncio 태스크로 기동 (`_modal_keep_warm_loop` 패턴). 주기마다:
  1. auto 구좌 3종을 자체 DB 신호로 계산해 ai.curation_sections upsert
     - popular          : 최근 7d ai.product_views 상위 (브랜드당 2개 캡)
     - trending-search  : 최근 7d ai.searches 결과셋에 가장 자주 등장한 상품
     - under-100        : 15,000~150,000 KRW 일상복 재고 상품, 조회수 순
       (v6 RPC에 가격 필터가 없어 검색이 아닌 DB 직접 쿼리 — 스펙 결정 사항.
       products.price는 KRW 저장. 상한은 실시간 USD 환율이 아니라 고정 15만원 —
       매일 흔들리는 상한에 실익이 없어 외부 FX API 의존을 제거했다)
  2. 노션 "큐레이션 구좌" DB → editorial 구좌 동기화 (NOTION_TOKEN 설정 시)
     - 활성 체크 해제 / 노션에서 사라진 페이지 → is_active=false

전체 fail-open: 어떤 단계가 실패해도 기존 행이 유지된다(stale-while-error).
GET /v1/curation은 이 테이블만 읽으므로 refresher 실패가 응답을 막지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

logger = logging.getLogger(__name__)

_GENDERS = ("women", "men")
_SECTION_SIZE = 20
_SIGNAL_WINDOW = "7 days"

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"

# products.gender 는 TEXT[] ('women'/'men'/'unisex' — crawler 어휘).
# unisex는 양쪽 구좌에 노출, gender 미기입 상품은 제외.
_GENDER_MATCH_SQL = "(%(gender)s = ANY(p.gender) OR 'unisex' = ANY(p.gender))"


# ── auto sections ─────────────────────────────────────────────────────────────

_POPULAR_SQL = f"""
    SELECT id FROM (
        SELECT p.id, v.cnt,
               ROW_NUMBER() OVER (PARTITION BY p.brand ORDER BY v.cnt DESC, p.id) AS rn
        FROM (
            SELECT product_id, count(*) AS cnt
            FROM ai.product_views
            WHERE viewed_at > now() - interval '{_SIGNAL_WINDOW}'
            GROUP BY product_id
        ) v
        JOIN public.products p ON p.id = v.product_id
        WHERE p.in_stock AND {_GENDER_MATCH_SQL}
    ) ranked
    WHERE rn <= 2
    ORDER BY cnt DESC, id
    LIMIT %(limit)s
"""  # noqa: S608 — 상수 interp only

_TRENDING_SQL = f"""
    SELECT p.id
    FROM (
        SELECT t.pid, count(*) AS cnt
        FROM ai.searches s
        CROSS JOIN LATERAL unnest(s.product_ids[1:10]) AS t(pid)
        WHERE s.created_at > now() - interval '{_SIGNAL_WINDOW}'
        GROUP BY t.pid
    ) x
    JOIN public.products p ON p.id = x.pid
    WHERE p.in_stock AND {_GENDER_MATCH_SQL}
    ORDER BY x.cnt DESC, p.id
    LIMIT %(limit)s
"""  # noqa: S608

# products.price는 항상 KRW로 저장됨 (crawler/src/import-products.ts가 임포트 시점에
# crawler/src/lib/fx.ts FX_TO_KRW로 USD/EUR/GBP -> KRW 변환 후 적재).
_PRICE_MIN_KRW = 15000
_PRICE_MAX_KRW = 150000

# "일상적인 옷" 구좌 — swimwear/underwear/jewelry/headwear/eyewear/accessories 배제.
_EVERYDAY_CATEGORIES = ["tops", "bottoms", "dresses", "outerwear", "knitwear", "shoes", "bags"]

# 카테고리가 tops/bottoms로 오분류된 수영복이 남아 이름 레벨에서 한 번 더 거른다.
_SWIM_NAME_RE = "(bikini|비키니|swimsuit|래쉬가드|rashguard|monokini)"

# 하한(15,000원)은 크롤 데이터 품질 가드다. price < 1,000원 행 ~2.7k건은 외화가
# KRW로 오탐되어 미변환 저장된 것이고(예: $145 -> price=145), 정확히 10,000원인
# 행들은 예약 상품의 예약금이 판매가 자리에 수집된 것이다. brand 3종 가드도 같은
# 성격 — 파싱 실패로 "판매가 : 125,000" 같은 레이블이 brand에 들어간 행을 막는다.
# 근본 원인은 크롤러 쪽이며 별도 작업으로 추적한다.
_UNDER_100_SQL = f"""
    SELECT id FROM (
        SELECT p.id, v.cnt,
               ROW_NUMBER() OVER (PARTITION BY p.brand ORDER BY v.cnt DESC NULLS LAST, p.id) AS rn
        FROM public.products p
        LEFT JOIN (
            SELECT product_id, count(*) AS cnt
            FROM ai.product_views
            WHERE viewed_at > now() - interval '{_SIGNAL_WINDOW}'
            GROUP BY product_id
        ) v ON v.product_id = p.id
        WHERE p.in_stock
          AND p.price BETWEEN {_PRICE_MIN_KRW} AND {_PRICE_MAX_KRW}
          AND p.category = ANY(%(categories)s)
          AND p.name !~* '{_SWIM_NAME_RE}'
          AND p.brand IS NOT NULL AND btrim(p.brand) <> '' AND p.brand !~ ':' AND p.brand <> p.name
          AND p.image_url IS NOT NULL
          AND {_GENDER_MATCH_SQL}
    ) ranked
    WHERE rn <= 2
    ORDER BY cnt DESC NULLS LAST, md5(id::text || current_date::text)
    LIMIT %(limit)s
"""  # noqa: S608 — 상수 interp only

_AUTO_SECTIONS: tuple[tuple[str, str, str, str | None, int], ...] = (
    # (section_id, sql, title, subtitle, sort_order)
    ("popular", _POPULAR_SQL, "지금 인기 브랜드", "요즘 가장 많이 보는 스타일", 1),
    ("trending-search", _TRENDING_SQL, "지금 뜨는 검색", "다른 사람들이 찾은 아이템", 2),
    ("under-100", _UNDER_100_SQL, "Under $100", "부담 없는 가격, 확실한 스타일", 3),
)


async def _upsert_section(
    pool: AsyncConnectionPool,
    *,
    section_id: str,
    gender: str,
    slot_type: str,
    title: str,
    subtitle: str | None,
    product_ids: list[int],
    sort_order: int,
    is_active: bool = True,
) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.curation_sections
                (section_id, gender, slot_type, title, subtitle, product_ids, sort_order, is_active, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (section_id, gender) DO UPDATE SET
                slot_type = EXCLUDED.slot_type,
                title = EXCLUDED.title,
                subtitle = EXCLUDED.subtitle,
                product_ids = EXCLUDED.product_ids,
                sort_order = EXCLUDED.sort_order,
                is_active = EXCLUDED.is_active,
                updated_at = now()
            """,
            (section_id, gender, slot_type, title, subtitle, product_ids, sort_order, is_active),
        )
        await conn.commit()


async def refresh_auto_sections(pool: AsyncConnectionPool) -> int:
    """auto 구좌 계산 + upsert. 결과가 빈 구좌는 건드리지 않는다 (기존 행 유지)."""
    written = 0
    # under-100 SQL만 categories를 참조 — 다른 섹션 SQL에는 해당 named param이
    # 없으므로 딕셔너리에 같이 넘겨도 무시된다.
    params = {"limit": _SECTION_SIZE, "categories": _EVERYDAY_CATEGORIES}
    for gender in _GENDERS:
        for section_id, sql, title, subtitle, sort_order in _AUTO_SECTIONS:
            try:
                async with pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(sql, {"gender": gender, **params})
                    product_ids = [int(r[0]) for r in await cur.fetchall()]
                if not product_ids:
                    logger.info("🗂 [curation] %s/%s empty — keep existing row", section_id, gender)
                    continue
                await _upsert_section(
                    pool,
                    section_id=section_id,
                    gender=gender,
                    slot_type="auto",
                    title=title,
                    subtitle=subtitle,
                    product_ids=product_ids,
                    sort_order=sort_order,
                )
                written += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("🗂 [curation] auto %s/%s failed: %r", section_id, gender, exc)
    return written


# ── editorial (Notion sync) ───────────────────────────────────────────────────


def _notion_plain_text(prop: dict[str, Any] | None) -> str:
    """title/rich_text property → concatenated plain text."""
    if not prop:
        return ""
    fragments = prop.get("title") or prop.get("rich_text") or []
    return "".join(f.get("plain_text", "") for f in fragments).strip()


def _parse_product_ids(raw: str) -> list[int]:
    """'123, 456 789' → [123, 456, 789]. product_id 리스트 형식 협의 전 관대한 파서."""
    return [int(tok) for tok in re.findall(r"\d+", raw)][:_SECTION_SIZE]


def _parse_notion_page(page: dict[str, Any]) -> dict[str, Any] | None:
    """노션 "큐레이션 구좌" 행 → 구좌 dict. 필수(구좌명) 누락 시 None."""
    props = page.get("properties", {})

    def _get(*names: str) -> dict[str, Any] | None:
        for name in names:
            if name in props:
                return props[name]
        return None

    title = _notion_plain_text(_get("구좌명", "title", "Name"))
    if not title:
        return None
    subtitle = _notion_plain_text(_get("서브타이틀", "subtitle")) or None
    order_prop = _get("순서", "order") or {}
    sort_order = int(order_prop.get("number") or 100)
    active_prop = _get("활성", "active") or {}
    is_active = bool(active_prop.get("checkbox", True))
    product_ids = _parse_product_ids(_notion_plain_text(_get("상품", "products")))
    gender_prop = _get("성별", "gender") or {}
    gender_sel = ((gender_prop.get("select") or {}).get("name") or "").strip().lower()
    genders = [gender_sel] if gender_sel in _GENDERS else list(_GENDERS)  # 성별 미지정 → 양쪽 노출

    return {
        "section_id": f"editorial-{page['id'].replace('-', '')[:12]}",
        "title": title,
        "subtitle": subtitle,
        "sort_order": sort_order,
        "is_active": is_active,
        "product_ids": product_ids,
        "genders": genders,
    }


async def _fetch_notion_pages() -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            body: dict[str, Any] = {"page_size": 50}
            if cursor:
                body["start_cursor"] = cursor
            resp = await client.post(
                f"{_NOTION_API}/databases/{settings.NOTION_CURATION_DB_ID}/query",
                headers={
                    "Authorization": f"Bearer {settings.NOTION_TOKEN}",
                    "Notion-Version": _NOTION_VERSION,
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")


async def sync_editorial_sections(pool: AsyncConnectionPool) -> int:
    """노션 editorial 구좌 동기화. 노션 미설정이면 no-op."""
    if not (settings.NOTION_TOKEN and settings.NOTION_CURATION_DB_ID):
        return 0

    pages = await _fetch_notion_pages()
    seen_ids: list[str] = []
    written = 0
    for page in pages:
        parsed = _parse_notion_page(page)
        if parsed is None:
            continue
        seen_ids.append(parsed["section_id"])
        for gender in parsed["genders"]:
            await _upsert_section(
                pool,
                section_id=parsed["section_id"],
                gender=gender,
                slot_type="editorial",
                title=parsed["title"],
                subtitle=parsed["subtitle"],
                product_ids=parsed["product_ids"],
                sort_order=parsed["sort_order"],
                is_active=parsed["is_active"],
            )
            written += 1

    # 노션에서 삭제된 editorial 구좌는 비활성화 (auto 구좌는 건드리지 않음).
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE ai.curation_sections
            SET is_active = false, updated_at = now()
            WHERE slot_type = 'editorial' AND NOT (section_id = ANY(%s)) AND is_active
            """,
            (seen_ids or [""],),
        )
        await conn.commit()
    return written


# ── loop ──────────────────────────────────────────────────────────────────────


async def refresh_once(pool: AsyncConnectionPool) -> None:
    auto_n = await refresh_auto_sections(pool)
    try:
        editorial_n = await sync_editorial_sections(pool)
    except Exception as exc:  # noqa: BLE001
        editorial_n = 0
        logger.warning("🗂 [curation] notion sync failed: %r", exc)
    logger.info("🗂 [curation] refresh done · auto=%d editorial=%d", auto_n, editorial_n)


async def curation_refresh_loop() -> None:
    """lifespan 백그라운드 태스크 본체. 시작 직후 1회 + 주기 반복."""
    interval = max(60, settings.CURATION_REFRESH_INTERVAL_S)
    logger.info("🗂 [curation] refresh loop started · interval=%ds", interval)
    while True:
        try:
            from app.providers import db_pool

            pool = db_pool._pool  # noqa: SLF001 — lifespan에서 init 실패 시 None
            if pool is None:
                logger.info("🗂 [curation] db pool unavailable — skip cycle")
            else:
                await refresh_once(pool)
        except asyncio.CancelledError:
            logger.info("🗂 [curation] refresh loop cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("🗂 [curation] refresh cycle raised: %r", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("🗂 [curation] refresh loop cancelled")
            raise
