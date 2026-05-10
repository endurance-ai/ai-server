import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api import router
from app.channels import link_resolver
from app.channels.factory import get_adapter, reset_adapter
from app.channels.session import init_store, shutdown_store
from app.channels.taste_profile import init_taste_store, shutdown_taste_store
from app.channels.telegram.adapter import TelegramAdapter
from app.channels.telegram.webhook import setup_webhook
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
    if settings.DB_URL and settings.DB_TOKEN:
        await SupabaseProvider.get_client()

    await init_store()
    if settings.TASTE_PROFILE_ENABLED:
        await init_taste_store()
    adapter = get_adapter()

    public_url = os.getenv("TELEGRAM_PUBLIC_URL", "").strip()
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if isinstance(adapter, TelegramAdapter) and public_url and secret:
        try:
            await setup_webhook(adapter, public_url, secret)
        except Exception:
            logging.getLogger(__name__).exception("setup_webhook failed")

    yield

    # Shutdown
    await shutdown_taste_store()
    await shutdown_store()
    await reset_adapter()
    await link_resolver.aclose()
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
