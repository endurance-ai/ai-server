import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import ORJSONResponse

from app.core.auth import verify_internal_token
from app.models.request import RecommendRequest
from app.models.response import RecommendResponse
from app.pipeline.runner import run_pipeline

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/recommend",
    response_model=RecommendResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def recommend(req: RecommendRequest) -> ORJSONResponse:
    logger.info(
        "[STEP 4.1][recommend] 라우터 진입 — item_id=%s subcategory=%s gender=%s tolerance=%.2f hasImage=%s",
        req.item.id,
        req.item.subcategory,
        req.gender,
        req.tolerance,
        bool(req.image_url),
    )
    try:
        resp = await run_pipeline(req)
    except Exception:
        # 내부 예외 메시지(SQL/Modal URL/스택)를 응답에 노출하지 않음 — 로그/Langfuse trace 만 남김
        logger.exception("[STEP 4.X][recommend] ❌ pipeline 예외 item_id=%s", req.item.id)
        raise HTTPException(status_code=502, detail="pipeline_failed") from None
    logger.info(
        "[STEP 4.10][recommend] ✅ 응답 — item_id=%s results=%d",
        req.item.id,
        len(resp.results),
    )
    return ORJSONResponse(content=resp.model_dump(by_alias=True))
