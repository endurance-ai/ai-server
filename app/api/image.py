"""Authenticated image-analysis API for first-party app clients.

This is a thin transport adapter around the shared Vision extractor. Images
are uploaded through ``POST /v1/uploads`` and the resulting public URL is sent
here for structured fashion-item analysis.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_id
from app.channels.schemas import _ssrf_guard_url
from app.channels.vision import extract as vision_extract

router = APIRouter(prefix="/v1/image", tags=["image"])


class ImageAnalyzeRequest(BaseModel):
    image_url: str = Field(min_length=4, description="Public image URL from POST /v1/uploads")


class ImageAnalyzePosition(BaseModel):
    top: float
    left: float
    # 항목 바운딩 박스 크기(이미지 대비 %) — 앱이 정확 크롭에 사용.
    width: float = 0.0
    height: float = 0.0


class ImageAnalyzeItem(BaseModel):
    name: str
    search_query_ko: str = Field(serialization_alias="searchQueryKo")
    search_query: str = Field(serialization_alias="searchQuery")
    category: str
    subcategory: str
    fit: str
    color: str
    position: ImageAnalyzePosition

    model_config = {"populate_by_name": True}


class ImageAnalyzeResponse(BaseModel):
    items: list[ImageAnalyzeItem]
    mood_tags: list[str]
    style_node: str


@router.post("/analyze", response_model=ImageAnalyzeResponse, status_code=status.HTTP_200_OK)
async def analyze_image(
    body: ImageAnalyzeRequest,
    _: UUID = Depends(get_current_user_id),
) -> ImageAnalyzeResponse:
    try:
        _ssrf_guard_url(body.image_url)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"ssrf_blocked: {exc}") from exc

    result = await vision_extract(body.image_url)

    return ImageAnalyzeResponse(
        items=[
            ImageAnalyzeItem(
                name=item.name,
                search_query_ko=item.searchQueryKo,
                search_query=item.searchQuery,
                category=item.category,
                subcategory=item.subcategory,
                fit=item.fit,
                color=item.color,
                position=ImageAnalyzePosition(
                    top=item.position.top,
                    left=item.position.left,
                    width=item.position.width,
                    height=item.position.height,
                ),
            )
            for item in result.items
        ],
        mood_tags=[tag.label for tag in result.mood.tags if tag.label],
        style_node=result.styleNode.primary,
    )
