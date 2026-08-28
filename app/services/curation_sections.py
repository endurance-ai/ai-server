"""Canonical curation section identifiers and metadata."""

from __future__ import annotations

from typing import Final

CURATION_SECTION_PRODUCT_LIMIT: Final = 100

DAILY_AUTO_SECTION_IDS: Final = ("trending-search", "under-100")

PERSONALIZED_AUTO_SECTIONS: Final = (
    {
        "section_id": "recently-viewed",
        "title": "최근 본 상품",
        "subtitle": None,
        "sort_order": 3,
    },
    {
        "section_id": "saved-on-sale",
        "title": "방금 할인된 찜",
        "subtitle": None,
        "sort_order": 4,
    },
    {
        "section_id": "taste-picks",
        "title": "취향 저격 아이템",
        "subtitle": None,
        "sort_order": 5,
    },
    {
        "section_id": "favorite-brands",
        "title": "내가 좋아하는 브랜드",
        "subtitle": None,
        "sort_order": 6,
    },
)

PERSONALIZED_AUTO_SECTION_IDS: Final = tuple(section["section_id"] for section in PERSONALIZED_AUTO_SECTIONS)
AUTO_SECTION_IDS: Final = DAILY_AUTO_SECTION_IDS + PERSONALIZED_AUTO_SECTION_IDS
