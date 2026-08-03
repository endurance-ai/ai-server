"""Demo-mode fixture candidates for POC video shoot.

Activated by `settings.DEMO_MODE=true`. Bypasses Modal/DB and returns these
hardcoded items as if they came from `search_products_v6`. Same Candidate
schema, same downstream send_results rendering — only the data source differs.

Triggered fixture: "약간 핏한 크롭 반팔 티셔츠" (Notion source).
"""

from __future__ import annotations

from app.models.response import Candidate


def _c(
    id_: str,
    brand: str,
    name: str,
    price: int,
    image_url: str,
    product_url: str,
    platform: str,
    score: float,
) -> Candidate:
    return Candidate(
        id=f"demo-{id_}",
        brand=brand,
        name=name,
        price=price,
        image_url=image_url,
        product_url=product_url,
        platform=platform,
        subcategory="t-shirt",
        score=score,
        dense_rank=None,
        sparse_rank=None,
    )


DEMO_SLIM_CROP_TEE: list[Candidate] = [
    _c(
        id_="ca-frank-baby-tee",
        brand="California Arts",
        name="Frank Baby Tee — Washed Black",
        price=82000,
        image_url="https://california-arts.com/cdn/shop/files/CA-FALL24-1549-Edit-Edit.jpg?v=1727383170",
        product_url="https://california-arts.com/collections/collection-t-shirts-tanks/products/frankbabytee?color=washed+black",
        platform="California Arts",
        score=0.94,
    ),
    _c(
        id_="bordr-base-tee",
        brand="Bōrdr",
        name="Base T-Shirts (Black)",
        price=59000,
        image_url="https://8division.com/web/product/big/202605/6a24a7132f031d8e1bf3b55b2e3cce00.jpg",
        product_url="https://8division.com/product/detail.html?product_no=65342&cate_no=2692&display_group=1",
        platform="8division",
        score=0.91,
    ),
    _c(
        id_="xlim-01-tee",
        brand="XLIM",
        name="01 T-SHIRT",
        price=140000,
        image_url="https://xlim.link/web/product/big/202604/eceef17ef754a352ef858ea6437adcc4.jpg",
        product_url="https://xlim.link/product/xlim-01-t-shirt/1687/category/73/display/1/",
        platform="XLIM",
        score=0.88,
    ),
    _c(
        id_="dbsr-blp-muscle-fit",
        brand="DBSR.",
        name="BLP Muscle Fit T-Shirt (Black)",
        price=36000,
        image_url="https://dumbstruck.kr/web/product/big/202603/b3bc65310acd5ce85fb8baf0be9ad00b.jpg",
        product_url="https://dumbstruck.kr/product/dbsr-blp-muscle-fit-t-shirt-black/2005/category/43/display/1/",
        platform="Dumbstruck",
        score=0.85,
    ),
    _c(
        id_="yuji-slim-tee",
        brand="유지 (YUJI)",
        name="남성 슬림핏 반팔 티셔츠 — 블랙",
        price=85600,
        image_url="https://tradingempty01.cafe24.com/web/_mig/product/big/260417_yjim261nrts030bk.jpg",
        product_url="https://empty.seoul.kr/product/%EC%9C%A0%EC%A7%80-%EB%82%A8%EC%84%B1-%EC%8A%AC%EB%A6%BC%ED%95%8F-%EB%B0%98%ED%8C%94-%ED%8B%B0%EC%85%94%EC%B8%A0-%EB%B8%94%EB%9E%99/45705/category/24/display/1/",
        platform="EMPTY Seoul",
        score=0.82,
    ),
]


def get_demo_candidates() -> list[Candidate]:
    """Return a fresh copy of the demo candidate list."""
    return list(DEMO_SLIM_CROP_TEE)


# ── Vision fixture ────────────────────────────────────────────────────────
# 데모용 고정 Vision 결과 — 실제 핀 이미지와 무관하게 항상 동일한 3 아이템.
# "약간 핏한 반팔 티셔츠" 시나리오에 맞춰 상의가 첫 번째.


def get_demo_vision_result():
    """Fixed VisionResult for the POC video shoot. No LLM call."""
    from app.channels.vision import (
        VisionItem,
        VisionMood,
        VisionResult,
        VisionStyle,
        VisionStyleNode,
    )

    return VisionResult(
        isApparel=True,
        styleNode=VisionStyleNode(primary="C", primaryConfidence=0.9, secondary="D", secondaryConfidence=0.4),
        sensitivityTags=["minimal", "summer"],
        mood=VisionMood(summary="여름 미니멀 시티 룩", vibe="clean minimal", season="summer", occasion="casual"),
        palette=[],
        style=VisionStyle(fit="slim", aesthetic="minimal", detectedGender="men"),
        items=[
            VisionItem(
                id="demo-item-1",
                category="top",
                subcategory="t-shirt",
                name="Fitted Grey T-Shirt",
                detail="짧은 소매, 크루넥, 슬림한 실루엣",
                fabric="cotton",
                color="grey",
                colorHex="#9a9a9a",
                colorFamily="GREY",
                fit="slim",
                searchQuery="men slim fit grey cotton t-shirt",
                searchQueryKo="남성 슬림핏 그레이 코튼 반팔 티셔츠",
            ),
            VisionItem(
                id="demo-item-2",
                category="bottom",
                subcategory="wide-pants",
                name="Wide-Leg Trousers",
                detail="릴렉스한 차콜 와이드 팬츠",
                fabric="cotton",
                color="charcoal",
                colorHex="#3a3a3a",
                colorFamily="GREY",
                fit="relaxed",
                searchQuery="men relaxed charcoal wide leg trousers",
                searchQueryKo="남성 릴렉스 차콜 와이드 팬츠",
            ),
            VisionItem(
                id="demo-item-3",
                category="shoes",
                subcategory="sneakers",
                name="White Leather Sneakers",
                detail="레귤러 화이트 가죽 스니커즈",
                fabric="leather",
                color="white",
                colorHex="#f5f5f5",
                colorFamily="WHITE",
                fit="regular",
                searchQuery="men white leather minimal sneakers",
                searchQueryKo="남성 화이트 가죽 미니멀 스니커즈",
            ),
        ],
    )
