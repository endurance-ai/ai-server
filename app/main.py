import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api import router
from app.core.config import settings
from app.providers.database import SupabaseProvider
from app.providers.embedding import EmbedProvider
from app.providers.llm import LLMProvider

# DIAG: 진단용 logging 셋업 — 검색 파이프라인 분석 끝나면 정리
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("app").setLevel(logging.DEBUG)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Startup — 비동기 클라이언트 워밍업으로 첫 요청 race condition 회피
    if settings.SUPABASE_URL and settings.SUPABASE_SERVICE_ROLE_KEY:
        await SupabaseProvider.get_client()
    yield
    # Shutdown
    await SupabaseProvider.close()
    await EmbedProvider.close()
    await LLMProvider.close()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
