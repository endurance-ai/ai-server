from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient


@pytest.fixture
async def edit_shop_rows(pool):
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("ALTER TABLE public.products ADD COLUMN IF NOT EXISTS product_no BIGINT")
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.edit_shop_profiles (
                platform TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                description TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            )
            """
        )
        await cur.execute(
            """
            INSERT INTO public.edit_shop_profiles (platform, display_name, description)
            VALUES ('slowsteadyclub', 'SLOW STEADY CLUB', 'description')
            ON CONFLICT (platform) DO UPDATE SET is_active = TRUE
            """
        )
        await cur.execute("DELETE FROM ai.edit_shop_rankings WHERE platform = 'slowsteadyclub'")
        await cur.executemany(
            """
            INSERT INTO public.products
                (brand, name, category, price, image_url, product_url, in_stock,
                 platform, gender, product_no)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'slowsteadyclub', %s, %s)
            RETURNING id
            """,
            [
                (
                    "A",
                    "Women outer",
                    "outerwear",
                    10000,
                    "https://img/1",
                    "https://slowsteadyclub.com/p/1?x=1#d",
                    True,
                    ["women"],
                    1,
                ),
                ("B", "Unisex outer", "outerwear", 20000, "https://img/2", "https://shop/p/2", True, ["unisex"], 2),
                ("C", "Men top", "tops", 30000, "https://img/3", "https://shop/p/3", True, ["men"], 3),
                ("D", "Too cheap", "tops", 4999, "https://img/4", "https://shop/p/4", True, ["women"], 4),
                ("E", "Sold out", "tops", 9000, "https://img/5", "https://shop/p/5", False, ["women"], 5),
                (
                    "F",
                    "Women accessory",
                    "accessories",
                    15000,
                    "https://img/6",
                    "https://shop/p/6",
                    True,
                    ["women"],
                    6,
                ),
            ],
        )
        await cur.execute("SELECT id, product_no FROM public.products ORDER BY product_no")
        ids = {int(product_no): int(product_id) for product_id, product_no in await cur.fetchall()}
        await cur.executemany(
            """
            INSERT INTO ai.edit_shop_rankings
                (platform, gender, category, product_id, final_rank, ranking_source, generated_for)
            VALUES ('slowsteadyclub', 'women', %s, %s, %s, 'what100_plus_shuffle', %s)
            """,
            [
                ("all", ids[1], 1, date(2026, 9, 15)),
                ("all", ids[2], 2, date(2026, 9, 15)),
                ("outerwear", ids[1], 1, date(2026, 9, 15)),
                ("outerwear", ids[2], 2, date(2026, 9, 15)),
            ],
        )
        await conn.commit()
    return ids


@pytest.mark.asyncio
async def test_filters_apply_quality_and_include_unisex(client: AsyncClient, edit_shop_rows) -> None:
    response = await client.get("/v1/edit-shops/slowsteadyclub/filters", params={"gender": "women"})
    assert response.status_code == 200
    body = response.json()
    assert body["shop"]["display_name"] == "SLOW STEADY CLUB"
    assert body["selected_gender"] == "women"
    assert {item["key"]: item["count"] for item in body["genders"]} == {"women": 3, "men": 2}
    assert [(item["key"], item["count"]) for item in body["categories"]] == [
        ("all", 3),
        ("outerwear", 2),
    ]


@pytest.mark.asyncio
async def test_products_use_frozen_rank_cursor_and_affiliate_url(client: AsyncClient, edit_shop_rows) -> None:
    first = await client.get(
        "/v1/edit-shops/slowsteadyclub/products",
        params={"gender": "women", "limit": 1},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert [item["name"] for item in first_body["items"]] == ["Women outer"]
    assert first_body["items"][0]["generated_for"] == "2026-09-15"
    assert first_body["items"][0]["external_url"] == (
        "https://slowsteadyclub.com/p/1?x=1&utm_source=kiko&utm_medium=referral&utm_campaign=slowsteadyclub#d"
    )
    assert first_body["next_cursor"]

    second = await client.get(
        "/v1/edit-shops/slowsteadyclub/products",
        params={"gender": "women", "limit": 1, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200
    assert [item["name"] for item in second.json()["items"]] == ["Unisex outer"]
    assert second.json()["next_cursor"] is None


@pytest.mark.asyncio
async def test_product_cursor_cannot_be_reused_for_another_filter(client: AsyncClient, edit_shop_rows) -> None:
    first = await client.get(
        "/v1/edit-shops/slowsteadyclub/products",
        params={"gender": "women", "limit": 1},
    )
    response = await client.get(
        "/v1/edit-shops/slowsteadyclub/products",
        params={"gender": "women", "category": "outerwear", "cursor": first.json()["next_cursor"]},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_curation_returns_optional_edit_shop_destination(client: AsyncClient, pool) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.curation_sections
                (section_id, gender, slot_type, display_type, title, product_ids,
                 sort_order, destination_type, destination_key)
            VALUES ('edit-shop-kith', 'women', 'editorial', 'trending', 'KITH', '{}',
                    10, 'edit_shop', 'kith')
            """
        )
        await conn.commit()
    response = await client.get("/v1/curation", params={"gender": "women"})
    assert response.status_code == 200
    section = response.json()["sections"][0]
    assert section["display_type"] == "trending"
    assert section["destination_type"] == "edit_shop"
    assert section["destination_key"] == "kith"
