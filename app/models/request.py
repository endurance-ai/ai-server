from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.core.config import settings


class StyleNode(BaseModel):
    primary: str
    secondary: str | None = None


class AnalyzedItem(BaseModel):
    """kikoai/app(Next.js)이 GPT-4o-mini Vision으로 검출한 단일 아이템."""

    id: str
    category: str
    subcategory: str | None = None
    name: str | None = None
    fit: str | None = None
    fabric: str | None = None
    # 2026-08-31 — 명시 속성 필터(rerank target). feature_metadata.pattern/neckline
    # 과 정렬. fit/fabric 과 함께 "우와 비슷하다" 디테일 축.
    pattern: str | None = None
    neckline: str | None = None
    # 2026-08-31 — v2.6 enrichment 축(product_features_v26.attr). 하의/길이 디테일.
    length: str | None = None
    sleeve_length: str | None = None
    leg_shape: str | None = None
    # 2026-09-01 — v2.6 스타일 무드 태그(product_features_v26.final_tags). 후보풀 하드필터.
    mood: str | None = None
    # 2026-09-01 — v2.6 스타일 디테일축(product_features_v26.attr). surface/texture/design_details.
    surface: str | None = None
    texture: str | None = None
    design_details: str | None = None
    # 2026-09-01 — v2.6 비어패럴 조건부축(신발/가방/안경/주얼리). rerank 소프트 가산.
    heel_type: str | None = None
    heel_height: str | None = None
    shaft: str | None = None
    shoe_toe: str | None = None
    bag_size: str | None = None
    bag_structure: str | None = None
    frame_shape: str | None = None
    metal_tone: str | None = None
    color_family: str | None = Field(default=None, alias="colorFamily")
    # 특정 상품/모델 지목 시 상품명 trigram 매칭어 (예: '2021M', 'trompe l’oeil').
    name_query: str | None = None
    search_query: str = Field(alias="searchQuery")
    search_query_ko: str | None = Field(default=None, alias="searchQueryKo")

    model_config = {"populate_by_name": True}


class PriceFilter(BaseModel):
    min_price: int | None = Field(default=None, alias="minPrice")
    max_price: int | None = Field(default=None, alias="maxPrice")

    model_config = {"populate_by_name": True}


class RecommendRequest(BaseModel):
    """AI 서버 메인 진입점 입력. kikoai/app의 /api/find/search가 이 모양으로 호출."""

    item: AnalyzedItem
    image_url: str = Field(alias="imageUrl", description="검색 기준 슬라이드 R2 URL")
    brand_filter: list[str] | None = Field(default=None, alias="brandFilter")
    gender: str | None = None
    style_node: StyleNode | None = Field(default=None, alias="styleNode")
    mood_tags: list[str] | None = Field(default=None, alias="moodTags")
    price_filter: PriceFilter | None = Field(default=None, alias="priceFilter")
    tolerance: float = Field(default=0.5, ge=0.0, le=1.0)
    final_limit: int | None = Field(default=None, alias="finalLimit", ge=1, le=50)

    @field_validator("image_url")
    @classmethod
    def _validate_image_url(cls, v: str) -> str:
        """SSRF 방지 — settings.ALLOWED_IMAGE_HOSTS 의 suffix 와 일치해야 함.
        ALLOWED_IMAGE_HOSTS 가 비어있으면(dev) 검증 스킵.
        """
        allowed = settings.allowed_image_hosts
        if not allowed:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("image_url scheme must be http or https")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError("image_url has no host")
        if not any(host == a or host.endswith(f".{a}") for a in allowed):
            raise ValueError(f"image_url host '{host}' not in allowlist")
        return v

    model_config = {"populate_by_name": True}
