"""Feedback API.

POST /v1/feedback — 피드백 제출
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, field_validator

from app.api.deps import get_current_user_id
from app.core.di import provide_db_pool
from app.services.curation_taste import record_search_result_signals

router = APIRouter(prefix="/v1", tags=["feedback"])

_VALID_REASONS = {
    "mood_off",
    "fit",
    "color",
    "category_wrong",
    "too_expensive",
    "dead_link",
    "too_similar",
    "mood_match",
    "new_brand",
    "price_good",
    "fit_color_good",
    "discovery",
}
_VALID_RATINGS = {"positive", "negative"}


class FeedbackRequest(BaseModel):
    search_id: UUID | None = None
    rating: str
    reasons: list[str] = []
    comment: str = ""
    consent: bool = False

    @field_validator("rating")
    @classmethod
    def validate_rating(cls, v: str) -> str:
        if v not in _VALID_RATINGS:
            raise ValueError(f"rating must be one of {sorted(_VALID_RATINGS)}")
        return v

    @field_validator("reasons")
    @classmethod
    def validate_reasons(cls, v: list[str]) -> list[str]:
        invalid = set(v) - _VALID_REASONS
        if invalid:
            raise ValueError(f"invalid reasons: {invalid}")
        return v


class FeedbackResponse(BaseModel):
    feedback_id: str
    exported_to_training: bool


@router.post("/feedback", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    body: FeedbackRequest,
    user_id: UUID = Depends(get_current_user_id),
    pool: AsyncConnectionPool = Depends(provide_db_pool),
) -> FeedbackResponse:
    """피드백 제출. consent=true 시 훈련 데이터 export 대상으로 표시."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO ai.feedbacks (user_id, search_id, rating, reasons, comment, consent)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING feedback_id
            """,
            (user_id, body.search_id, body.rating, body.reasons, body.comment, body.consent),
        )
        row = await cur.fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to save feedback")

    if body.search_id is not None:
        await record_search_result_signals(
            pool,
            user_id=user_id,
            search_id=body.search_id,
            signal_type=("conversation_like" if body.rating == "positive" else "conversation_dislike"),
        )

    return FeedbackResponse(feedback_id=str(row[0]), exported_to_training=False)
