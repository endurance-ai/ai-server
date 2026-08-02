"""Onboarding API.

POST /v1/onboarding — 로그인 완료 시 온보딩 로컬값(성별 + 브랜드 픽) 계정 저장 (auth required)

신규·재로그인 공통 — 재로그인도 재확인 후 갱신이므로 picks는 전체 교체(replace) 방식.
프론트의 수기 지정 그리드 픽이든 직접 검색 추가든 클라이언트는 brand_id 만 보내고,
서버가 brand_nodes.primary_style_node_id 로 노드를 유도해 {스타일, 브랜드} 쌍으로 저장한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool
from app.core.gender import app_to_db
from app.services.curation_taste import record_style_signal

router = APIRouter(prefix="/v1", tags=["onboarding"])

_MAX_BRAND_PICKS = 50


class OnboardingRequest(BaseModel):
    gender: Literal["women", "men"]
    selected_brand_ids: list[int] = Field(default_factory=list, max_length=_MAX_BRAND_PICKS)


class OnboardingResponse(BaseModel):
    user_id: str
    gender: Literal["women", "men"]
    saved_brand_ids: list[int]


@router.post("/onboarding", response_model=OnboardingResponse, status_code=status.HTTP_200_OK)
async def save_onboarding(
    body: OnboardingRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> OnboardingResponse:
    """성별 + 브랜드 픽 저장. 존재하지 않는 brand_id는 조용히 제외 (클라 로컬 캐시가 stale할 수 있음)."""
    brand_ids = list(dict.fromkeys(body.selected_brand_ids))  # dedupe, 순서 보존

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE ai.user_profiles
            SET gender = %s, onboarded_at = now(), updated_at = now()
            WHERE user_id = %s
            RETURNING user_id
            """,
            (app_to_db(body.gender), user_id),
        )
        if not await cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        saved: list[int] = []
        node_by_brand: dict[int, int | None] = {}
        if brand_ids:
            await cur.execute(
                "SELECT id, primary_style_node_id FROM public.brand_nodes WHERE id = ANY(%s)",
                (brand_ids,),
            )
            node_by_brand = {r[0]: (int(r[1]) if r[1] is not None else None) for r in await cur.fetchall()}
            saved = [b for b in brand_ids if b in node_by_brand]

        # 재확인 플로우 = 전체 교체: 이전 온보딩 픽을 지우고 이번 선택으로 갱신.
        await cur.execute(
            "DELETE FROM ai.user_brand_picks WHERE user_id = %s AND source = 'onboarding'",
            (user_id,),
        )
        for brand_id in saved:
            await cur.execute(
                """
                INSERT INTO ai.user_brand_picks (user_id, brand_id, style_node_id, source)
                VALUES (%s, %s, %s, 'onboarding')
                ON CONFLICT (user_id, brand_id) DO NOTHING
                """,
                (user_id, brand_id, node_by_brand.get(brand_id)),
            )
        await conn.commit()

    for style_node_id in sorted({n for n in node_by_brand.values() if n is not None}):
        await record_style_signal(
            pool,
            user_id=user_id,
            style_node_id=style_node_id,
            signal_type="onboarding",
            dedupe_key=f"onboarding:{user_id}:{style_node_id}:{datetime.now(tz=UTC).date()}",
            metadata={"brand_ids": [b for b in saved if node_by_brand.get(b) == style_node_id]},
        )

    return OnboardingResponse(user_id=str(user_id), gender=body.gender, saved_brand_ids=saved)
