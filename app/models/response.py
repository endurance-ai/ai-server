from typing import Any

from pydantic import BaseModel, Field


class Candidate(BaseModel):
    """검색 결과 단건. product_id + 스코어 + 핵심 메타. 상세는 kikoai/app이 DB 재조회."""

    id: str
    brand: str
    name: str
    price: int | None = None
    image_url: str | None = Field(default=None, serialization_alias="imageUrl")
    product_url: str | None = Field(default=None, serialization_alias="productUrl")
    platform: str | None = None
    subcategory: str | None = None
    score: float
    dense_rank: int | None = Field(default=None, serialization_alias="denseRank")
    sparse_rank: int | None = Field(default=None, serialization_alias="sparseRank")
    # 색/소재/핏 등 VLM 속성. 내부 전용(클라 API 는 Candidate 를 직접 반환하지
    # 않음) — sess.last_results 로 실어, 유저가 카드를 핀했을 때 에이전트가 그
    # 상품의 특징(색/소재/핏)을 알 수 있게 한다(핀 상품 상세 미확보 버그 픽스).
    feature_metadata: dict[str, Any] | None = None

    model_config = {"populate_by_name": True}


class RecommendResponse(BaseModel):
    item_id: str = Field(serialization_alias="itemId")
    results: list[Candidate]
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="단계별 후보 수 (dense, sparse, fused, after_diversify, final)",
    )
    latency_ms: dict[str, int] = Field(
        default_factory=dict,
        serialization_alias="latencyMs",
        description="단계별 latency",
    )

    model_config = {"populate_by_name": True}
