import asyncio
import logging
import os
import time
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
from app.infrastructure.cache import chat_state
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
from app.providers.database import DatabaseProvider
from app.providers.embedding import EmbedProvider
from app.providers.llm import LLMProvider

# DIAG: 진단용 logging 셋업 — 검색 파이프라인 분석 끝나면 정리
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("app").setLevel(logging.DEBUG)


async def _modal_keep_warm_loop() -> None:
    """SPEC-MODAL-KEEP-WARM-001 — Modal `/embed` 콜드 스타트 방지용 배경 태스크.

    Modal 워크스페이스 `portal-embed` 는 `min_containers=0` (scale-to-zero) 로
    배포돼 있어 idle 후 첫 요청이 콜드 스타트 (~19-20s) 를 겪음. 2026-07-07
    실측 트레이스 (`modal.embed_image.modal_ms`) 기준:
    - 콜드: 19,785ms / 19,789ms / 15,063ms
    - 웜: 3,207ms / 454ms / 406ms

    현재 `AGENT_TOOL_TIMEOUT_S=20` 에 딱 걸려 재시도가 발동하는데, 재시도가
    또 다른 콜드 컨테이너를 때리는 경우 (04:00 카고 쇼츠 트레이스) 총
    ~35-45초로 늘어남. 클라이언트 stall heartbeat 로 배너는 안 뜨지만
    유저 대기가 여전히 길다.

    근본 해결은 Modal 소스 (`embed_app.py`) 에 `min_containers=1` 을
    지정하는 것이지만, 소스가 hchsa77 개인 워크스페이스에만 존재하고
    org 이전 진행 중이라 우리 쪽에서는 접근 불가. 그동안의 실용 조치로
    `check_connection` (`/health` GET) 를 주기적으로 호출해 Modal 의
    `scaledown_window` (기본 5분) 를 리셋 → 컨테이너 계속 warm 유지.

    Fail-open: 모든 예외는 조용히 삼킴 (Modal 다운은 별개 알람 채널로
    감지). 태스크 자체는 lifespan 종료 시 `cancel()` 로 정리.
    """
    from app.observability.langfuse import start_as_current_span, update_current_span

    log = logging.getLogger(__name__)
    interval = max(30, int(settings.MODAL_KEEP_WARM_INTERVAL_S))
    timeout = max(5.0, float(settings.MODAL_KEEP_WARM_TIMEOUT_S))
    log.info("🔥 [modal.keepwarm] loop started · interval=%ds timeout=%.1fs", interval, timeout)
    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("🔥 [modal.keepwarm] loop cancelled")
            return
        try:
            with start_as_current_span("modal.keepwarm"):
                _t0 = time.perf_counter()
                ok = await asyncio.wait_for(EmbedProvider.check_connection(), timeout=timeout)
                _ms = int((time.perf_counter() - _t0) * 1000)
                update_current_span(metadata={"modal_ms": _ms, "ok": ok})
            if ok:
                log.info("🔥 [modal.keepwarm] ping ok · ⏱ modal=%dms", _ms)
            else:
                log.warning("🔥 [modal.keepwarm] ping returned ok=False · ⏱ modal=%dms", _ms)
        except asyncio.CancelledError:
            log.info("🔥 [modal.keepwarm] loop cancelled")
            return
        except Exception as exc:  # noqa: BLE001 — never crash the loop
            log.warning("🔥 [modal.keepwarm] ping raised: %r", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Startup — 비동기 클라이언트 워밍업으로 첫 요청 race condition 회피
    if settings.DB_URL and settings.DB_TOKEN:
        await DatabaseProvider.get_client()

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

    # SPEC-CHAT-STATE-REDIS-001 — warm the chat-state redis pool. Fail-open:
    # any failure logs INFO and returns False; helpers fall back to safe
    # defaults so card delivery is never blocked by redis unavailability.
    await chat_state.warm_pool()

    # SPEC-SEARCH-V6-STYLE-WIRING — populate the style_node code→id cache so
    # search_service can resolve Vision letters to bigint ids without a
    # per-search round-trip. Fail-open: keeps the hardcoded A..U=1..21 fallback.
    from app.infrastructure.repositories.style_node import warm_cache as warm_style_node_cache

    await warm_style_node_cache()

    # SPEC-PERSONALIZE-RERANK — populate brand_nodes.attributes cache so the
    # rerank step can look up vibe / price_tier / gender_lean per candidate
    # without a per-search SELECT. Fail-open: cache stays empty → rerank
    # degrades to a no-op (RPC order preserved).
    from app.infrastructure.repositories.brand_node_cache import warm_cache as warm_brand_cache

    await warm_brand_cache()

    # SPEC-BRAND-2TOWER-RESCORE — populate brand_multimodal_embeddings cache
    # (~6.7 MB for 2,180 brands @ 768 dim). Used by the 2-tower rescore step
    # in search_service. Fail-open: cache empty → rescore is a no-op.
    from app.infrastructure.repositories.brand_embedding_cache import warm_cache as warm_brand_emb_cache

    await warm_brand_emb_cache()

    adapter = get_adapter()

    public_url = os.getenv("TELEGRAM_PUBLIC_URL", "").strip()
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if isinstance(adapter, TelegramAdapter) and public_url and secret:
        try:
            await setup_webhook(adapter, public_url, secret)
        except Exception:
            logging.getLogger(__name__).exception("setup_webhook failed")

    # SPEC-MODAL-KEEP-WARM-001 — start the Modal keep-warm background task.
    # `MODAL_KEEP_WARM_ENABLED=false` in dev/local when a live Modal endpoint
    # isn't configured. Task handle is stashed on app.state so shutdown can
    # cancel it cleanly (a stray asyncio task blocks graceful shutdown).
    if settings.MODAL_KEEP_WARM_ENABLED:
        app.state.modal_keep_warm_task = asyncio.create_task(_modal_keep_warm_loop())
    else:
        app.state.modal_keep_warm_task = None
        logging.getLogger(__name__).info("🔥 [modal.keepwarm] disabled (MODAL_KEEP_WARM_ENABLED=false)")

    yield

    # Shutdown
    kw_task = getattr(app.state, "modal_keep_warm_task", None)
    if kw_task is not None:
        kw_task.cancel()
        try:
            await kw_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort cleanup
            pass
    await shutdown_taste_store()
    await shutdown_store()
    await db_pool.close_pool()
    await reset_adapter()
    await link_resolver.aclose()
    await DatabaseProvider.close()
    await EmbedProvider.close()
    await LLMProvider.close()
    # SPEC-CHAT-STATE-REDIS-001 — close the chat-state redis pool. Fail-open.
    await chat_state.close_pool()
    # SPEC-OBSERVABILITY-002 / REQ-OBS-COST-001 — drain Langfuse background queue.
    langfuse_flush()


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
