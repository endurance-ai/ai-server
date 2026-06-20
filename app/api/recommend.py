import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import ORJSONResponse

from app.core.auth import verify_internal_token
from app.models.request import RecommendRequest
from app.models.response import RecommendResponse
from app.pipeline.runner import run_pipeline

logger = logging.getLogger(__name__)

router = APIRouter()


# Dead external caller (kikoai/app on own v6 path post SPEC-SEARCH-V6-001);
# retained, fed by shared v6 repo (the same SearchRepository the Telegram bot
# agent path uses — migrating the shared repository covers both).
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
    # Frame a cost-accounting turn so enhance_query (and any other pipeline LLM
    # call) is attributed instead of silently no-op'd (audit finding #4).
    from app.observability.turn_cost import reset_turn

    reset_turn()
    try:
        resp = await run_pipeline(req)
    except Exception:
        # 내부 예외 메시지(SQL/Modal URL/스택)를 응답에 노출하지 않음 — 로그/Langfuse trace 만 남김
        logger.exception("[STEP 4.X][recommend] ❌ pipeline 예외 item_id=%s", req.item.id)
        raise HTTPException(status_code=502, detail="pipeline_failed") from None
    finally:
        try:
            from app.observability.conversation_log import emit
            from app.observability.turn_cost import get_turn_totals

            turn = get_turn_totals()
            if turn["cost_usd"] > 0:
                emit(
                    event_type="turn_summary",
                    user_key=f"recommend:{req.item.id}",
                    chat_id=0,
                    thread_id=None,
                    turn_no=None,
                    payload={
                        "rec_id": None,
                        "status": "responded",
                        "exit_reason": "recommend_endpoint",
                        "total_tokens": turn["total_tokens"],
                        "cost_usd": round(turn["cost_usd"], 8),
                        "cache_read_tokens": turn["cache_read_tokens"],
                        "cache_creation_tokens": turn.get("cache_creation_tokens", 0),
                        "by_source": turn.get("by_source") or {},
                    },
                )
        except Exception:  # noqa: BLE001 — observability must never block
            logger.debug("[recommend] turn_summary emit best-effort skip", exc_info=True)
    logger.info(
        "[STEP 4.10][recommend] ✅ 응답 — item_id=%s results=%d",
        req.item.id,
        len(resp.results),
    )
    return ORJSONResponse(content=resp.model_dump(by_alias=True))
