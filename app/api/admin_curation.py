"""큐레이션 구좌 어드민 — ai.curation_sections 직접 편집.

노션 "큐레이션 구좌 (어드민)" DB 를 대체한다. 노션 동기화는 스냅샷 전체가
전부-아니면-전무로 거부되는 데다(활성 3~6개, editorial 정확히 12개 …) 실패가
로그 한 줄로만 남아 운영자가 반영 여부를 알 수 없었다. 여기서는 저장이 곧
반영이고, 저장 직후 실제로 노출될 상품 수를 돌려준다.

소유 경계 (curation_refresh.py 와 나눠 가진다):
  어드민(이 모듈) — 구좌의 존재·제목·순서·활성·display_type, editorial 상품 목록
  리프레셔        — 공용 auto 구좌(trending-search / under-100)의 product_ids

그래서 auto 구좌의 product_ids 는 여기서 편집할 수 없다. 리프레셔가 KST 하루
한 번 덮어쓰므로 편집해도 사라진다 — 필드 자체를 막아 혼란을 없앤다.

UI 는 kiko.ai-app 어드민(`/admin/curation`)이 소유한다 — 이 모듈은 데이터와
검증 로직만 제공한다. `/debug/*` 를 쓰는 검색 디버거와 같은 구조다.

Auth: 표준 `verify_internal_token`. dev(INTERNAL_API_TOKEN 미설정)에선 통과.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app.core.auth import verify_internal_token
from app.core.di import provide_db_pool
from app.services.curation_refresh import GENDER_MATCH_SQL, PRODUCT_FEATURES_JOIN
from app.services.curation_sections import AUTO_SECTION_IDS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/curation", tags=["admin"], dependencies=[Depends(verify_internal_token)])

# 서버가 상품을 계산하는 구좌 — product_ids 편집 금지.
_AUTO_SECTION_IDS = AUTO_SECTION_IDS

Gender = Literal["women", "men"]
SlotType = Literal["auto", "editorial"]
DisplayType = Literal["default", "trending"]


# ── Schemas ───────────────────────────────────────────────────────────────────


class SectionPayload(BaseModel):
    """구좌 한 행. PK 는 (section_id, gender)."""

    section_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    gender: Gender
    slot_type: SlotType
    display_type: DisplayType = "default"
    title: str = Field(min_length=1, max_length=200)
    subtitle: str | None = Field(default=None, max_length=200)
    sort_order: int = Field(default=100, ge=0, le=9999)
    is_active: bool = False
    # auto 구좌에서는 무시된다 — 리프레셔 소유.
    product_ids: list[int] = Field(default_factory=list, max_length=200)


class SectionRow(SectionPayload):
    """조회 응답. `live_count` 는 지금 이 구좌에서 실제 카드로 뜨는 상품 수다.

    저장한 product_ids 중 품절·이미지 없음·가격 미달·성별 불일치를 뺀 값이라
    운영자가 저장 직후 실제 노출 수를 알 수 있다.
    """

    live_count: int = 0


class SectionListResponse(BaseModel):
    sections: list[SectionRow]


class DeleteResponse(BaseModel):
    deleted: bool


class SectionOrderPayload(BaseModel):
    """한 성별의 전체 구좌 순서. 누락된 목록은 부분 재정렬로 보지 않고 거부한다."""

    gender: Gender
    section_ids: list[str] = Field(min_length=1, max_length=200)


class ReorderResponse(BaseModel):
    updated: int


class PreviewSection(BaseModel):
    section_id: str
    gender: Gender
    display_type: DisplayType
    title: str
    sort_order: int
    shown: int


class PreviewResponse(BaseModel):
    """피드 순서대로 훑으며 중복 제거까지 반영한 실제 노출 수."""

    gender: Gender
    sections: list[PreviewSection]


class ProductRow(BaseModel):
    product_id: int
    brand: str
    name: str
    price: float | None
    image_url: str
    in_stock: bool
    eligible: bool
    # eligible=false 인 첫 번째 사유. `eligible` 과 같은 SQL 술어에서 계산되므로
    # 둘이 어긋날 수 없다. 어드민이 "왜 안 뜨는지" 를 카드에 바로 띄운다.
    ineligible_reason: Literal["out_of_stock", "no_image", "price_too_low", "gender_mismatch"] | None = None


class ProductLookupResponse(BaseModel):
    products: list[ProductRow]
    missing: list[int]


# ── Helpers ───────────────────────────────────────────────────────────────────

_SELECT_COLUMNS = "section_id, gender, slot_type, display_type, title, subtitle, sort_order, is_active, product_ids"


def _row_to_section(row: tuple[Any, ...]) -> SectionRow:
    return SectionRow(
        section_id=row[0],
        gender=row[1],
        slot_type=row[2],
        display_type=row[3],
        title=row[4],
        subtitle=row[5],
        sort_order=row[6],
        is_active=row[7],
        product_ids=[int(p) for p in (row[8] or [])],
    )


async def _live_count(cur: Any, product_ids: list[int], gender: str) -> int:
    """app/api/curation.py 하이드레이션과 같은 술어로 센다."""
    if not product_ids:
        return 0
    await cur.execute(
        f"""
        SELECT count(*)
        FROM public.products p
        {PRODUCT_FEATURES_JOIN}
        WHERE p.id = ANY(%(ids)s)
          AND p.in_stock
          AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
          AND p.price >= 5000
          AND {GENDER_MATCH_SQL}
        """,  # noqa: S608 -- 보간되는 값은 모두 모듈 소유 상수
        {"ids": product_ids, "gender": gender},
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/sections", response_model=SectionListResponse)
async def list_sections(
    gender: Gender | None = Query(default=None),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> SectionListResponse:
    """구좌 전체. 비활성 포함 — 어드민은 꺼둔 구좌도 봐야 한다."""
    where = "WHERE gender = %(gender)s" if gender else ""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT {_SELECT_COLUMNS}
            FROM ai.curation_sections
            {where}
            ORDER BY sort_order ASC, section_id ASC, gender ASC
            """,  # noqa: S608 -- where 절은 모듈 소유 상수
            {"gender": gender},
        )
        sections = [_row_to_section(row) for row in await cur.fetchall()]
        for section in sections:
            section.live_count = await _live_count(cur, section.product_ids, section.gender)
    return SectionListResponse(sections=sections)


@router.put("/sections", response_model=SectionRow)
async def upsert_section(
    body: SectionPayload,
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> SectionRow:
    """구좌 저장. (section_id, gender) 기준 upsert.

    auto 구좌의 product_ids 는 무시하고 기존값을 보존한다. 공용 auto는
    리프레셔가, 개인화 auto는 요청 경로가 상품을 계산한다.
    """
    product_ids = list(dict.fromkeys(body.product_ids))
    if body.slot_type == "auto" and body.section_id not in _AUTO_SECTION_IDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"auto slot_type requires section_id in {_AUTO_SECTION_IDS} (서버가 계산할 수 없다)",
        )

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            INSERT INTO ai.curation_sections
                (section_id, gender, slot_type, display_type, title, subtitle,
                 product_ids, sort_order, is_active, updated_at)
            VALUES (%(section_id)s, %(gender)s, %(slot_type)s, %(display_type)s, %(title)s,
                    %(subtitle)s, %(product_ids)s, %(sort_order)s, %(is_active)s, now())
            ON CONFLICT (section_id, gender) DO UPDATE SET
                slot_type = EXCLUDED.slot_type,
                display_type = EXCLUDED.display_type,
                title = EXCLUDED.title,
                subtitle = EXCLUDED.subtitle,
                product_ids = CASE
                    WHEN EXCLUDED.slot_type = 'auto' THEN ai.curation_sections.product_ids
                    ELSE EXCLUDED.product_ids
                END,
                sort_order = EXCLUDED.sort_order,
                is_active = EXCLUDED.is_active,
                updated_at = now()
            RETURNING {_SELECT_COLUMNS}
            """,  # noqa: S608 -- RETURNING 컬럼은 모듈 소유 상수
            {
                "section_id": body.section_id,
                "gender": body.gender,
                "slot_type": body.slot_type,
                "display_type": body.display_type,
                "title": body.title,
                "subtitle": body.subtitle,
                "product_ids": [] if body.slot_type == "auto" else product_ids,
                "sort_order": body.sort_order,
                "is_active": body.is_active,
            },
        )
        saved = _row_to_section(await cur.fetchone())
        saved.live_count = await _live_count(cur, saved.product_ids, saved.gender)
        await conn.commit()
    logger.info("🗂 [admin] section saved · %s/%s live=%d", saved.section_id, saved.gender, saved.live_count)
    return saved


@router.patch("/sections/order", response_model=ReorderResponse)
async def reorder_sections(
    body: SectionOrderPayload,
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> ReorderResponse:
    """현재 성별의 전체 구좌 순서를 한 트랜잭션으로 저장한다."""
    if len(body.section_ids) != len(set(body.section_ids)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="section_ids must not contain duplicates",
        )

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT section_id FROM ai.curation_sections WHERE gender = %s FOR UPDATE",
            (body.gender,),
        )
        existing_ids = {str(row[0]) for row in await cur.fetchall()}
        requested_ids = set(body.section_ids)
        if requested_ids != existing_ids:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="curation sections changed; reload before reordering",
            )

        for sort_order, section_id in enumerate(body.section_ids, start=1):
            await cur.execute(
                """
                UPDATE ai.curation_sections
                SET sort_order = %s, updated_at = now()
                WHERE section_id = %s AND gender = %s
                """,
                (sort_order, section_id, body.gender),
            )
        await conn.commit()

    logger.info("🗂 [admin] sections reordered · %s count=%d", body.gender, len(body.section_ids))
    return ReorderResponse(updated=len(body.section_ids))


@router.delete("/sections/{section_id}/{gender}", response_model=DeleteResponse)
async def delete_section(
    section_id: str,
    gender: Gender,
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> DeleteResponse:
    """행 삭제. 되돌릴 수 없으니 보통은 is_active=false 를 쓴다."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM ai.curation_sections WHERE section_id = %s AND gender = %s",
            (section_id, gender),
        )
        deleted = cur.rowcount > 0
        await conn.commit()
    return DeleteResponse(deleted=deleted)


@router.get("/preview", response_model=PreviewResponse)
async def preview_feed(
    gender: Gender = Query(),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> PreviewResponse:
    """앱에 실제로 뜨는 구좌별 상품 수.

    `display_type='trending'` 구좌끼리만 중복을 뺀다.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT section_id, slot_type, display_type, title, sort_order, product_ids
            FROM ai.curation_sections
            WHERE gender = %s AND is_active
            ORDER BY sort_order ASC, section_id ASC
            """,
            (gender,),
        )
        rows = await cur.fetchall()
        trending_section_ids: set[int] = set()
        sections: list[PreviewSection] = []
        for section_id, slot_type, display_type, title, sort_order, product_ids in rows:
            candidates = [int(p) for p in (product_ids or [])]
            is_trending = display_type == "trending"
            remaining = [pid for pid in candidates if not is_trending or pid not in trending_section_ids]
            if is_trending:
                trending_section_ids.update(remaining)
            sections.append(
                PreviewSection(
                    section_id=section_id,
                    gender=gender,
                    display_type=display_type,
                    title=title,
                    sort_order=sort_order,
                    shown=await _live_count(cur, remaining, gender),
                )
            )
    return PreviewResponse(gender=gender, sections=sections)


@router.get("/products", response_model=ProductLookupResponse)
async def lookup_products(
    ids: str = Query(description="쉼표/공백 구분 product_id 목록"),
    gender: Gender = Query(),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> ProductLookupResponse:
    """붙여넣은 ID 목록을 카드로 확인. `eligible=false` 면 그 구좌에서 안 뜬다."""
    parsed: list[int] = []
    for token in ids.replace(",", " ").split():
        if token.isdigit():
            parsed.append(int(token))
    parsed = list(dict.fromkeys(parsed))[:200]
    if not parsed:
        return ProductLookupResponse(products=[], missing=[])

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT p.id, p.brand, p.name, p.price, p.image_url, p.in_stock,
                   (p.in_stock
                    AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
                    AND p.price >= 5000
                    AND {GENDER_MATCH_SQL}) AS eligible,
                   CASE
                       WHEN p.in_stock IS NOT TRUE THEN 'out_of_stock'
                       WHEN p.image_url IS NULL OR btrim(p.image_url) = '' THEN 'no_image'
                       WHEN p.price IS NULL OR p.price < 5000 THEN 'price_too_low'
                       WHEN ({GENDER_MATCH_SQL}) IS NOT TRUE THEN 'gender_mismatch'
                       ELSE NULL
                   END AS ineligible_reason
            FROM public.products p
            {PRODUCT_FEATURES_JOIN}
            WHERE p.id = ANY(%(ids)s)
            """,  # noqa: S608 -- 보간되는 값은 모두 모듈 소유 상수
            {"ids": parsed, "gender": gender},
        )
        by_id = {
            int(r[0]): ProductRow(
                product_id=int(r[0]),
                brand=r[1] or "",
                name=r[2] or "",
                price=float(r[3]) if r[3] is not None else None,
                image_url=r[4] or "",
                in_stock=bool(r[5]),
                eligible=bool(r[6]),
                ineligible_reason=r[7],
            )
            for r in await cur.fetchall()
        }
    # 붙여넣은 순서를 유지한다 — 운영자가 정렬 의도를 갖고 넣는다.
    return ProductLookupResponse(
        products=[by_id[pid] for pid in parsed if pid in by_id],
        missing=[pid for pid in parsed if pid not in by_id],
    )
