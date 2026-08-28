"""Brand-backed curation section seed behavior."""

from __future__ import annotations

import pytest

from scripts.seed_curation_sections import _expand_brands
from tests.test_auth.test_curation_onboarding_api import _insert_brand, _insert_product


@pytest.mark.asyncio
async def test_brand_section_expands_all_products_from_a_single_brand(pool):
    brand_id = await _insert_brand(pool, "Deep Catalog")
    product_ids = [
        await _insert_product(
            pool,
            brand="Deep Catalog",
            brand_node_id=brand_id,
            name=f"Product {index}",
        )
        for index in range(35)
    ]

    async with pool.connection() as conn, conn.cursor() as cur:
        expanded = await _expand_brands(cur, [brand_id], "women", excluded=set())

    assert len(expanded) == 35
    assert set(expanded) == set(product_ids)
