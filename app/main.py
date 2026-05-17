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
from app.channels.telegram.adapter import TelegramAdapter
from app.channels.telegram.webhook import setup_webhook
from app.core.config import settings
from app.infrastructure.memory.session import (
    InMemorySessionStore,
    init_store,
    set_store_factory,
    shutdown_store,
)
from app.infrastructure.memory.session_pg import PostgresSessionStore
from app.infrastructure.memory.taste_profile import (
    InMemoryTasteProfileStore,
    init_taste_store,
    set_taste_store_factory,
    shutdown_taste_store,
)
from app.infrastructure.memory.taste_profile_pg import PostgresTasteProfileStore
from app.observability.langfuse import flush as langfuse_flush
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

    # SPEC-CONVERSATION-LOG-001 / LOG-T07 — reachability probe for
    # ai.log_conversation_event. Sets `app.state.conv_log_backend` to
    # "postgres" on success, "stderr" on failure (e.g., migration not applied).
    # log_event() reads this flag to decide between INSERT and stderr fallback.
    await _probe_conversation_log(app)

    await init_store()
    if settings.TASTE_PROFILE_ENABLED:
        await init_taste_store()
    adapter = get_adapter()

    # SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-ENTRY-001 cascade — Apify warmup.
    # @MX:SPEC: SPEC-ONBOARD-CARDS-001
    _warmup_apify_provider()

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
    # SPEC-OBSERVABILITY-002 / REQ-OBS-COST-001 — drain Langfuse background queue.
    langfuse_flush()


# @MX:NOTE: [AUTO] SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-ENTRY-001 — Apify
# warmup logger. The provider is stateless (httpx-on-demand) but operators
# need a startup-time signal whether scrape calls will succeed or degrade.
# @MX:SPEC: SPEC-ONBOARD-CARDS-001
def _warmup_apify_provider() -> None:
    log = logging.getLogger(__name__)
    token = (getattr(settings, "APIFY_TOKEN", "") or "").strip()
    actor = getattr(settings, "APIFY_PINTEREST_ACTOR", "")
    if token:
        # Token redacted — never log raw token bytes (REQ-ONBOARD-SEC-001).
        log.info("🎨 [APIFY] provider armed actor=%s token_len=%d", actor, len(token))
    else:
        log.info("🎨 [APIFY] degraded — APIFY_TOKEN empty; board/profile scrapes will return []")


async def _probe_conversation_log(app: FastAPI) -> None:
    """SPEC-CONVERSATION-LOG-001 / LOG-T07 — best-effort reachability probe.

    Runs ONLY after `_select_memory_backend()` succeeded with a Postgres pool.
    Sets `app.state.conv_log_backend`:
    - "postgres" → log_event() writes rows.
    - "stderr"   → log_event() emits single-line JSON to stderr (REQ-LOG-FAILSOFT-001).
    The probe never blocks lifespan startup — failure only flips the backend
    flag so individual emits degrade gracefully (R3: best-effort).
    """
    log = logging.getLogger(__name__)
    # Only probe when Postgres pool is live (i.e., we did NOT fall back to in-memory).
    pool = db_pool._pool  # noqa: SLF001 — internal, but only check for None
    if pool is None:
        app.state.conv_log_backend = "stderr"
        log.info("[CONV_LOG][startup] backend=stderr (no postgres pool)")
        return
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM ai.log_conversation_event LIMIT 0")
        app.state.conv_log_backend = "postgres"
        log.info("[CONV_LOG][startup] log_conversation_event reachable backend=postgres")
    except Exception as exc:  # noqa: BLE001
        app.state.conv_log_backend = "stderr"
        log.warning(
            "[CONV_LOG][startup] table unreachable (%s) — emits will fallback to stderr",
            type(exc).__name__,
        )


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
    # SPEC-IMPLICIT-FB-001 — module-level Postgres flag for implicit feedback fast-path.
    try:
        from app.channels.implicit_feedback import set_backend_is_postgres

        set_backend_is_postgres(True)
    except Exception:  # noqa: BLE001
        log.exception("[MEMORY][startup] failed to set implicit_feedback backend flag")
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
