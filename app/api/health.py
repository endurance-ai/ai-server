import asyncio
import os

from fastapi import APIRouter, Depends
from fastapi.responses import ORJSONResponse

from app.channels.factory import backend_name, get_adapter
from app.core.auth import verify_internal_token
from app.core.config import settings
from app.providers.database import SupabaseProvider
from app.providers.embedding import EmbedProvider
from app.providers.llm import LLMProvider

router = APIRouter()


@router.get("/health")
async def health_live() -> ORJSONResponse:
    """Liveness — 무인증, 부울만 반환. 인프라 LB/Docker healthcheck 용도."""
    return ORJSONResponse(content={"status": "ok", "version": settings.VERSION})


@router.get("/health/ready", dependencies=[Depends(verify_internal_token)])
async def health_ready() -> ORJSONResponse:
    """Readiness — 인증 필요. 의존 서비스 상태 상세 노출."""
    supabase_ok = await SupabaseProvider.check_connection()
    modal_ok = await EmbedProvider.check_connection()
    litellm_ok = await LLMProvider.check_connection()

    all_ok = supabase_ok and modal_ok and litellm_ok
    status_code = 200 if all_ok else 503

    backend = backend_name()
    bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "")
    reachable = False
    if backend == "telegram":
        try:
            adapter = get_adapter()
            me = await asyncio.wait_for(adapter.get_me(), timeout=3.0)
            if me:
                reachable = True
                bot_username = me.get("username") or bot_username
        except (TimeoutError, Exception):
            reachable = False

    return ORJSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if all_ok else "degraded",
            "supabase": "connected" if supabase_ok else "disconnected",
            "modal_embed": "connected" if modal_ok else "disconnected",
            "litellm": "connected" if litellm_ok else "disconnected",
            "messenger_backend": backend,
            "bot_username": bot_username,
            "reachable": reachable,
            "version": settings.VERSION,
        },
    )
