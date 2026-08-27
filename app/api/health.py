from fastapi import APIRouter, Depends
from fastapi.responses import ORJSONResponse

from app.core.auth import verify_internal_token
from app.core.config import settings
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.session_pg import PostgresSessionStore
from app.providers.database import DatabaseProvider
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
    database_ok = await DatabaseProvider.check_connection()
    modal_ok = await EmbedProvider.check_connection()
    litellm_ok = await LLMProvider.check_connection()

    all_ok = database_ok and modal_ok and litellm_ok
    status_code = 200 if all_ok else 503

    # SPEC-MEMORY-001 REQ-MEMORY-HEALTH-001 — surface active memory backend
    memory_backend = "postgres" if isinstance(get_store(), PostgresSessionStore) else "in_memory"

    # SPEC-AGENT-V2-CLEANUP-001 — the ReAct agent is the permanent topology.
    # The only remaining effective gate is whether AGENT_LLM_MODEL is set
    # (fail-closed in llm_client when empty).
    agent_llm_model_configured = bool(settings.AGENT_LLM_MODEL.strip())
    agent_v2_react_effective = agent_llm_model_configured

    return ORJSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if all_ok else "degraded",
            "database": "connected" if database_ok else "disconnected",
            "modal_embed": "connected" if modal_ok else "disconnected",
            "litellm": "connected" if litellm_ok else "disconnected",
            "memory_backend": memory_backend,
            "agent_v2_react_enabled": agent_v2_react_effective,
            "agent_llm_model_configured": agent_llm_model_configured,
            "version": settings.VERSION,
        },
    )
