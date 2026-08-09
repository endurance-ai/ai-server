"""Visual Intelligence 진입 검색 API (Strategy B — authed first-light).

POST /v1/visual-search — 기존 `/v1/uploads` 로 만든 CloudFront image_url 을 받아
기존 v6 이미지 임베딩 → RPC 파이프라인(`run_image_search`)으로 랭킹 상품을 **동기**
반환한다. 채팅 SSE 그래프를 우회하는 저지연 진입점으로, iOS 26 Visual Intelligence
App Intent 전용이다.

NOTE(Strategy B): 지금은 인증 필수(`get_current_user_id`) — 로그인 유저의 개인화
rerank 까지 그대로 태운다. 게스트 무인증화 + 바이트 직수신(S3 서버측 put)은 후속
Strategy A 에서 다룬다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_id

router = APIRouter(prefix="/v1", tags=["visual-search"])


class VisualSearchRequest(BaseModel):
    image_url: str = Field(min_length=1, description="CloudFront URL from POST /v1/uploads")
    limit: int = Field(default=8, ge=1, le=30, description="Max ranked results")


class VisualSearchItem(BaseModel):
    id: str
    brand: str | None = None
    name: str | None = None
    price: float | None = None
    image_url: str | None = None
    product_url: str | None = None


class VisualSearchResponse(BaseModel):
    results: list[VisualSearchItem]


def _field(cand: Any, key: str) -> Any:
    """Candidate 는 pydantic 모델(photo path)이거나 raw dict 일 수 있다."""
    if isinstance(cand, dict):
        return cand.get(key)
    return getattr(cand, key, None)


@router.post("/visual-search", response_model=VisualSearchResponse, status_code=status.HTTP_200_OK)
async def visual_search(
    body: VisualSearchRequest,
    user_id: UUID = Depends(get_current_user_id),
) -> VisualSearchResponse:
    # 기존 photo-pick 경로를 그대로 재사용 — 새 알고리즘 없음(image_url → v6 RPC).
    from app.agents.tools.search_products import run_image_search

    cands = await run_image_search(
        image_url=body.image_url,
        text_query="fashion",  # v6 는 text param 이 없어 정보용(로그/메타)일 뿐.
        top_k=body.limit,
        user_key=str(user_id),  # SPEC-PERSONALIZE-RERANK — 로그인 유저 개인화 적용.
    )

    items: list[VisualSearchItem] = []
    for c in cands:
        pid = str(_field(c, "id") or _field(c, "product_id") or "")
        if not pid:
            continue
        items.append(
            VisualSearchItem(
                id=pid,
                brand=_field(c, "brand"),
                name=_field(c, "name"),
                price=_field(c, "price"),
                image_url=_field(c, "image_url"),
                product_url=_field(c, "product_url"),
            )
        )
    return VisualSearchResponse(results=items)
