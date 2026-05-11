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
from app.channels.session import (
    InMemorySessionStore,
    init_store,
    set_store_factory,
    shutdown_store,
)
from app.channels.session_pg import PostgresSessionStore
from app.channels.taste_profile import (
    InMemoryTasteProfileStore,
    init_taste_store,
    set_taste_store_factory,
    shutdown_taste_store,
)
from app.channels.taste_profile_pg import PostgresTasteProfileStore
from app.channels.telegram.adapter import TelegramAdapter
from app.channels.telegram.webhook import setup_webhook
from app.core.config import settings
from app.providers import db_pool
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

    # SPEC-MEMORY-001 — choose memory backend before factory injection
    await _select_memory_backend()

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
    await db_pool.close_pool()
    await reset_adapter()
    await link_resolver.aclose()
    await SupabaseProvider.close()
    await EmbedProvider.close()
    await LLMProvider.close()


async def _select_memory_backend() -> None:
    """SPEC-MEMORY-001 REQ-MEMORY-LIFECYCLE-001 / REQ-MEMORY-FALLBACK-001/002.

    Probe Postgres. On success, register Postgres factories. On failure:
    - dev (`MEMORY_FALLBACK_ON_PROBE_FAIL=true`): log + fall back to in-memory.
    - prod (`MEMORY_FALLBACK_ON_PROBE_FAIL=false`): re-raise → startup aborts.
    """
    log = logging.getLogger(__name__)
    if not settings.DB_DSN:
        log.warning("[MEMORY][startup] DB_DSN empty; using in-memory stores backend=in_memory")
        set_store_factory(InMemorySessionStore)
        set_taste_store_factory(InMemoryTasteProfileStore)
        return

    try:
        await db_pool.init_pool()
    except Exception as e:  # noqa: BLE001
        _handle_probe_failure(e, log)
        return

    set_store_factory(PostgresSessionStore)
    set_taste_store_factory(PostgresTasteProfileStore)
    log.info("[MEMORY][startup] backend=postgres")


def _handle_probe_failure(err: Exception, log: logging.Logger) -> None:
    safe_dsn = db_pool._sanitize_dsn(settings.DB_DSN)
    if settings.MEMORY_FALLBACK_ON_PROBE_FAIL:
        log.error(
            "[MEMORY][startup] postgres probe failed dsn=%s memory_backend=in_memory_fallback error=%s",
            safe_dsn,
            err,
        )
        set_store_factory(InMemorySessionStore)
        set_taste_store_factory(InMemoryTasteProfileStore)
        return
    log.error(
        "[MEMORY][startup] postgres probe failed dsn=%s fail_loud=true error=%s",
        safe_dsn,
        err,
    )
    raise err


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
