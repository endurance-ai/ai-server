"""Public edit-shop metadata, filters, and ranked product list APIs."""

from __future__ import annotations

import base64
import json
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from app.core.di import provide_db_pool
from app.services.edit_shop_rankings import ALL_CATEGORY, EDIT_SHOP_PLATFORMS
from app.services.outbound_url import with_partner_attribution

router = APIRouter(prefix="/v1/edit-shops", tags=["edit-shops"])
Gender = Literal["women", "men"]


class EditShopProfile(BaseModel):
    platform: str
    display_name: str
    description: str


class EditShopGenderOption(BaseModel):
    key: Gender
    count: int


class EditShopCategoryOption(BaseModel):
    key: str
    label: str
    count: int
    thumbnail_url: str | None


class EditShopFiltersResponse(BaseModel):
    shop: EditShopProfile
    selected_gender: Gender
    genders: list[EditShopGenderOption]
    categories: list[EditShopCategoryOption]


class EditShopProduct(BaseModel):
    id: int
    brand: str
    name: str
    price: float | None
    original_price: float | None
    sale_price: float | None
    image_url: str
    product_url: str
    external_url: str
    ranking_source: Literal["what100", "popular", "daily_shuffle", "what100_plus_shuffle"]
    generated_for: date


class EditShopProductsResponse(BaseModel):
    items: list[EditShopProduct]
    next_cursor: str | None


async def _profile(cur, platform: str) -> EditShopProfile:
    if platform not in EDIT_SHOP_PLATFORMS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Edit shop not found")
    await cur.execute(
        """
        SELECT platform, display_name, description
        FROM public.edit_shop_profiles
        WHERE platform = %s AND is_active
        """,
        (platform,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Edit shop not found")
    return EditShopProfile(platform=str(row[0]), display_name=str(row[1]), description=str(row[2]))


def _encode_cursor(*, platform: str, gender: str, category: str, generated_for: date, rank: int) -> str:
    payload = {"p": platform, "g": gender, "c": category, "d": generated_for.isoformat(), "r": rank}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, *, platform: str, gender: str, category: str) -> tuple[date, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
        if payload.get("p") != platform or payload.get("g") != gender or payload.get("c") != category:
            raise ValueError
        generated_for = date.fromisoformat(payload["d"])
        rank = int(payload["r"])
        if rank < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid cursor") from exc
    return generated_for, rank


@router.get("/{platform}/filters", response_model=EditShopFiltersResponse)
async def edit_shop_filters(
    platform: str,
    gender: Gender = Query(),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> EditShopFiltersResponse:
    async with pool.connection() as conn, conn.cursor() as cur:
        shop = await _profile(cur, platform)
        await cur.execute(
            """
            SELECT requested.gender, count(p.id)::int
            FROM unnest(ARRAY['women', 'men']::text[]) requested(gender)
            LEFT JOIN public.products p
              ON p.platform = %s
             AND p.in_stock
             AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
             AND p.price >= 5000
             AND p.gender && ARRAY[requested.gender, 'unisex']::text[]
            GROUP BY requested.gender
            ORDER BY array_position(ARRAY['women', 'men']::text[], requested.gender)
            """,
            (platform,),
        )
        genders = [EditShopGenderOption(key=row[0], count=int(row[1])) for row in await cur.fetchall()]
        await cur.execute(
            """
            WITH eligible AS (
                SELECT id, category, image_url
                FROM public.products
                WHERE platform = %s
                  AND in_stock
                  AND image_url IS NOT NULL AND btrim(image_url) <> ''
                  AND price >= 5000
                  AND gender && ARRAY[%s, 'unisex']::text[]
            ), grouped AS (
                SELECT category AS key, count(*)::int AS n,
                       (array_agg(image_url ORDER BY id DESC))[1] AS thumbnail
                FROM eligible
                WHERE category IS NOT NULL
                  AND btrim(category) <> ''
                  AND lower(btrim(category)) <> 'accessories'
                GROUP BY category
            )
            SELECT key, n, thumbnail FROM grouped ORDER BY key
            """,
            (platform, gender),
        )
        category_rows = await cur.fetchall()
        await cur.execute(
            """
            SELECT count(*)::int, (array_agg(image_url ORDER BY id DESC))[1]
            FROM public.products
            WHERE platform = %s
              AND in_stock
              AND image_url IS NOT NULL AND btrim(image_url) <> ''
              AND price >= 5000
              AND gender && ARRAY[%s, 'unisex']::text[]
            """,
            (platform, gender),
        )
        all_row = await cur.fetchone()

    categories = [
        EditShopCategoryOption(
            key=ALL_CATEGORY,
            label="전체",
            count=int(all_row[0]) if all_row else 0,
            thumbnail_url=all_row[1] if all_row else None,
        ),
        *(
            EditShopCategoryOption(key=str(row[0]), label=str(row[0]), count=int(row[1]), thumbnail_url=row[2])
            for row in category_rows
        ),
    ]
    return EditShopFiltersResponse(shop=shop, selected_gender=gender, genders=genders, categories=categories)


@router.get("/{platform}/products", response_model=EditShopProductsResponse)
async def edit_shop_products(
    platform: str,
    gender: Gender = Query(),
    category: str | None = Query(default=None, min_length=1, max_length=100),
    sort: Literal["recommended"] = Query(default="recommended"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> EditShopProductsResponse:
    del sort  # only one launch sort is intentionally supported by the schema
    category_key = category or ALL_CATEGORY
    async with pool.connection() as conn, conn.cursor() as cur:
        await _profile(cur, platform)
        if cursor:
            generated_for, after_rank = _decode_cursor(cursor, platform=platform, gender=gender, category=category_key)
        else:
            await cur.execute(
                """
                SELECT max(generated_for)
                FROM ai.edit_shop_rankings
                WHERE platform = %s AND gender = %s AND category = %s
                """,
                (platform, gender, category_key),
            )
            row = await cur.fetchone()
            generated_for = row[0] if row and row[0] is not None else None
            after_rank = 0
            if generated_for is None:
                return EditShopProductsResponse(items=[], next_cursor=None)

        await cur.execute(
            """
            SELECT p.id, p.brand, p.name, p.price, p.original_price, p.sale_price,
                   p.image_url, p.product_url, r.ranking_source, r.generated_for, r.final_rank
            FROM ai.edit_shop_rankings r
            JOIN public.products p ON p.id = r.product_id
            WHERE r.platform = %s AND r.gender = %s AND r.category = %s
              AND r.generated_for = %s AND r.final_rank > %s
              AND p.in_stock
              AND p.image_url IS NOT NULL AND btrim(p.image_url) <> ''
              AND p.price >= 5000
              AND p.gender && ARRAY[%s, 'unisex']::text[]
            ORDER BY r.final_rank
            LIMIT %s
            """,
            (platform, gender, category_key, generated_for, after_rank, gender, limit + 1),
        )
        rows = await cur.fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    items = [
        EditShopProduct(
            id=int(row[0]),
            brand=str(row[1]),
            name=str(row[2]),
            price=float(row[3]) if row[3] is not None else None,
            original_price=float(row[4]) if row[4] is not None else None,
            sale_price=float(row[5]) if row[5] is not None else None,
            image_url=str(row[6]),
            product_url=str(row[7]),
            external_url=with_partner_attribution(str(row[7])),
            ranking_source=row[8],
            generated_for=row[9],
        )
        for row in page
    ]
    next_cursor = (
        _encode_cursor(
            platform=platform,
            gender=gender,
            category=category_key,
            generated_for=page[-1][9],
            rank=int(page[-1][10]),
        )
        if has_more and page
        else None
    )
    return EditShopProductsResponse(items=items, next_cursor=next_cursor)
