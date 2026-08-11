from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.curation import CurationProduct
from app.api.products import _get_similar


def test_curation_product_exposes_original_and_sale_prices():
    product = CurationProduct(
        product_id=1,
        brand="SaleBrand",
        name="Sale Product",
        price=45000,
        original_price=70000,
        sale_price=45000,
        image_url="https://img.test/sale.jpg",
        product_url="https://shop.test/1",
    )

    payload = product.model_dump()
    assert payload["original_price"] == 70000.0
    assert payload["sale_price"] == 45000.0


@pytest.mark.asyncio
async def test_get_similar_returns_original_and_sale_prices():
    cursor = AsyncMock()
    cursor.fetchall.return_value = [
        (2, "SaleBrand", "Sale Product", 45000, 70000, 45000, "https://img.test/sale.jpg", "https://shop.test/2"),
        (3, "FullBrand", "Full Product", 50000, None, None, "https://img.test/full.jpg", "https://shop.test/3"),
    ]
    cursor_cm = MagicMock()
    cursor_cm.__aenter__ = AsyncMock(return_value=cursor)
    cursor_cm.__aexit__ = AsyncMock(return_value=None)
    conn = MagicMock()
    conn.cursor.return_value = cursor_cm
    conn_cm = MagicMock()
    conn_cm.__aenter__ = AsyncMock(return_value=conn)
    conn_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.connection.return_value = conn_cm

    products = await _get_similar(pool, product_id=1)

    assert products[0].original_price == 70000.0
    assert products[0].sale_price == 45000.0
    assert products[1].original_price is None
    assert products[1].sale_price is None
    query = cursor.execute.await_args.args[0]
    assert "p.original_price" in query
    assert "p.sale_price" in query
