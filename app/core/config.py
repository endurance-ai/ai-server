from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    PROJECT_NAME: str = "kiko-ai-server"
    VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    # DB (PostgREST shim — 검색 RPC + 상품 메타 조회)
    DB_URL: str = ""
    DB_TOKEN: str = ""

    # Modal — FashionSigLIP 임베딩 엔드포인트
    MODAL_EMBED_URL: str = ""
    MODAL_EMBED_TOKEN: str = ""  # Modal proxy auth (optional)
    MODAL_EMBED_TIMEOUT: float = 90.0  # cold start (모델 GPU 로드) 여유 — warm 시 1초 내

    # LiteLLM Proxy — LLM 호출 (enhance_query, future rerank)
    LITELLM_BASE_URL: str = "http://localhost:4000"
    LITELLM_MASTER_KEY: str = ""

    # Langfuse — 관측성
    LANGFUSE_HOST: str = "http://localhost:3000"
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    # SPEC-OBSERVABILITY-002 / REQ-OBS-COST-002 — emergency fallback to drop
    # @observe from 8 non-LLM-calling nodes (ingest/resolve_image/pick_item/
    # ask_clarify/apply_clarify/search/send_results/taste_update) when full
    # decoration exceeds the < 5ms p99 latency budget. Default off — restore
    # full coverage by leaving false.
    LANGFUSE_SELECTIVE_MODE: bool = False
    # P0 user-feedback scores — kill-switch for retro-scoring the original
    # recommendation trace from implicit feedback (click / no_click / re_query).
    # Default on; set false to silence all create_score() calls without
    # touching the feedback / webhook paths.
    LANGFUSE_FEEDBACK_SCORES: bool = True

    # 검색 파라미터 기본값
    SEARCH_DEFAULT_K: int = 50  # RPC top-k
    SEARCH_BRAND_CAP: int = 2  # 다양성: 브랜드당 최대
    SEARCH_PLATFORM_CAP: int = 3  # 다양성: 플랫폼당 최대
    SEARCH_FINAL_LIMIT: int = 15  # 최종 응답 개수

    # enhance_query — LLM 기반 sparse 검색 쿼리 정제 (SPEC-PIPELINE-001)
    # 안전 롤아웃: 기본 False. 운영 검증 후 .env 에서 true 로 전환.
    ENHANCE_QUERY_ENABLED: bool = False
    ENHANCE_QUERY_MODEL: str = "gpt-4o-mini"
    ENHANCE_QUERY_TIMEOUT_MS: int = 1500
    ENHANCE_QUERY_MAX_TOKENS: int = 200
    ENHANCE_QUERY_TEMPERATURE: float = 0.2
    # embed + enhance_query 병렬 실행 (asyncio.gather). false 시 sequential.
    PIPELINE_PARALLEL_ENABLED: bool = True

    # SPEC-MEMORY-001 — Postgres-backed memory persistence
    # ENV: deployment environment marker. "production" 일 때 fail-loud 정책 (REQ-MEMORY-FALLBACK-002).
    ENV: str = "development"
    # DB_DSN: psycopg3-style URL for the dev-app Postgres (distinct from DB_URL which is PostgREST shim).
    DB_DSN: str = ""
    MEMORY_POOL_MIN_SIZE: int = 2
    MEMORY_POOL_MAX_SIZE: int = 10
    # When true, fall back to InMemory stores if Postgres probe fails. Set to "false" in production.
    MEMORY_FALLBACK_ON_PROBE_FAIL: bool = True
    SESSION_CLEANUP_INTERVAL_S: int = 300

    # 보안
    # Next.js → AI 서버 shared secret. 비어있으면 인증 skip (dev). 운영에선 반드시 설정.
    INTERNAL_API_TOKEN: str = ""
    # SSRF 가드 — 콤마 구분 호스트 suffix 매칭. 비어있으면 검증 skip (dev).
    ALLOWED_IMAGE_HOSTS: str = ""

    # Messenger channel (SPEC-MSG-001)
    MESSENGER_BACKEND: str = "telegram"
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    TELEGRAM_API_BASE: str = "https://api.telegram.org"
    TELEGRAM_PUBLIC_URL: str = ""
    # AWS Bedrock 의 Nova Lite — LiteLLM proxy 에서 `nova-lite` 로 별칭 매핑됨
    # (aws-infra/kikoai-dev-servers/ai/config/litellm.yaml). OpenAI gpt-4o-mini
    # 의 429 Too Many Requests 문제 회피 + Bedrock 별도 quota + 비용 절감.
    VISION_MODEL: str = "nova-lite"
    BOT_LANGUAGE: str = "en"
    SESSION_TTL_SECONDS: int = 1800

    # Routing-LLM (paraphrase/intent classification + critique parsing)
    # Cheap fast model — separate from VISION/ENHANCE_QUERY to keep cost lines clear.
    ROUTER_MODEL: str = "gpt-4o-mini"
    # 1500ms 는 LiteLLM proxy + OpenAI roundtrip (300~2000ms) 에 너무 빡빡해서
    # 거의 매 턴 timeout fallback 으로 빠짐. 3000ms 로 완화. fallback 자체는
    # 안전망 역할로 유지 (REQ-LLM-004).
    ROUTER_TIMEOUT_MS: int = 3000
    ROUTER_MAX_TOKENS: int = 300
    # When the deterministic prefilter cannot classify a text message, fall back
    # to LLM routing. Disable to revert to pure SM behavior (router becomes no-op).
    ROUTER_LLM_ENABLED: bool = True

    # Long-term taste profile — persistent (in-memory for now; Redis later).
    # When false, scenario behaves as before (no profile read/write).
    TASTE_PROFILE_ENABLED: bool = True
    TASTE_PROFILE_TTL_SECONDS: int = 60 * 60 * 24 * 30  # 30 days

    # Critique — tap-button refinement on result cards
    CRITIQUE_CHEAPER_RATIO: float = 0.7  # "cheaper" = max_price = anchor * 0.7

    # SPEC-AGENT-001 — LangGraph respond/ask_clarify nodes (LiteLLM 경유 ChatOpenAI)
    RESPONSE_MODEL: str = "gpt-4o-mini"
    RESPONSE_TIMEOUT_MS: int = 5000
    RESPONSE_MAX_TOKENS: int = 200
    # noscroll 벤치마크 — 1문장씩 끊어서 발화하면 대화감이 살아남.
    # respond 노드가 LLM/fallback 출력을 문장 단위로 split → typing action → 짧은 딜레이 → send.
    RESPONSE_SPLIT_ENABLED: bool = True
    RESPONSE_SPLIT_DELAY_MS: int = 350
    RESPONSE_SPLIT_MIN_CHARS: int = 8  # 너무 짧은 조각은 다음 청크와 병합
    # Vision 결과가 약할 때(짧은 description / ambiguous label) ask_clarify 분기 트리거 임계값
    ASK_CLARIFY_MIN_DESC_TOKENS: int = 3
    ASK_CLARIFY_AMBIGUOUS_LABELS: str = "item,clothing,thing,piece"

    # SPEC-VISION-UNIFY-001 — Rich Vision schema (parity with kikoai/app analyze.ts)
    # Master flag for the new rich-schema behavior. When false, falls back to
    # the pre-migration minimal schema (REQ-VISION-COMPAT-005).
    VISION_SCHEMA_V2: bool = True
    # Raised from 600 to fit the rich schema (REQ-VISION-UNIFY-003).
    VISION_MAX_TOKENS: int = 2500
    # Raised from 0.2 to match kikoai/app run-vision.ts (REQ-VISION-UNIFY-003).
    VISION_TEMPERATURE: float = 0.3
    # Per-call timeout, raised from 15.0 to fit the larger response.
    VISION_TIMEOUT_S: float = 30.0
    # Weak-vision predicate thresholds (REQ-VISION-WEAKVISION-001).
    # Renames ASK_CLARIFY_MIN_DESC_TOKENS — the legacy var still works for one release.
    ASK_CLARIFY_MIN_QUERY_TOKENS: int = 4
    # Extends ASK_CLARIFY_AMBIGUOUS_LABELS — applied to subcategory in v2.
    ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES: str = "item,clothing,thing,piece"

    # SPEC-AGENTIC-CRITIQUE-001 — Self-critique loop ──────────────────────
    SELF_CRITIQUE_ENABLED: bool = True
    SELF_CRITIQUE_MAX_ITERATIONS: int = 2
    SELF_CRITIQUE_THRESHOLD: float = 0.6
    SELF_CRITIQUE_TIMEOUT_S: float = 30.0
    SELF_CRITIQUE_FASTPATH_DROP_FILTERS: str = "min_price,max_price,exclude_keywords"
    EVALUATOR_MODEL: str = "nova-lite"
    EVALUATOR_MAX_TOKENS: int = 400
    EVALUATOR_TEMPERATURE: float = 0.2
    EVALUATOR_TIMEOUT_S: float = 8.0

    # SPEC-CLARIFY-CARDS-001 — Inline-keyboard clarify cards ──────────────
    # 비활성 시 ask_clarify 는 본 SPEC 이전(자유 텍스트 LLM) 동작으로 회귀.
    CLARIFY_CARDS_ENABLED: bool = True
    # 카드당 버튼 상한(skip 포함). 범위 [3, 8].
    CLARIFY_MAX_BUTTONS: int = 5

    # Apify — Instagram post image scraping. Same actor + token as kikoai/app.
    # Empty token → Instagram links resolve to [] (P2-skip behavior preserved).
    # Token present → link_resolver._is_instagram branch dispatches to Apify.
    APIFY_TOKEN: str = ""
    # Apify actor id and sync timeout. Defaults match kikoai/app.
    APIFY_INSTAGRAM_ACTOR: str = "apify~instagram-post-scraper"
    APIFY_SYNC_TIMEOUT_S: int = 120
    # Httpx wall-clock cap for the whole Apify call (Apify sync timeout + α).
    APIFY_FETCH_TIMEOUT_S: float = 130.0
    # 402 (free credit exhausted) circuit-breaker cool-off — once hit, suppress
    # further Instagram resolution attempts for this many seconds to avoid log
    # spam and pointless network calls.
    APIFY_402_COOLOFF_S: int = 600

    # DEMO MODE — local-only POC short-circuit for video shoot.
    # When true, search_node bypasses DB/Modal and returns fixture candidates
    # from app.pipeline.demo_fixtures. Restore production by leaving false.
    DEMO_MODE: bool = False

    # SPEC-IMPLICIT-FB-001 — implicit feedback capture (impressions/clicks/re-query)
    # No-click attribution window. Snapshotted per-row at INSERT time. REQ-FB-NOCLICK-001.
    IMPLICIT_FB_ATTRIBUTION_WINDOW_S: int = 600
    # Re-query detection window after RESULTS_SENT. REQ-FB-REQUERY-001.
    IMPLICIT_FB_REQUERY_WINDOW_S: int = 90
    # Soft-negative weight applied on no-click attribution. REQ-FB-NOCLICK-001.
    IMPLICIT_FB_NOCLICK_WEIGHT: float = 0.2
    # Strong-positive weight applied on "👀 자세히" click. REQ-FB-CLICK-001.
    IMPLICIT_FB_CLICK_WEIGHT: float = 1.0
    # Soft-negative weight applied on rapid re-query. REQ-FB-REQUERY-001.
    IMPLICIT_FB_REQUERY_WEIGHT: float = 0.5

    # SPEC-AGENT-V2-CLEANUP-001 — ReAct agent loop is now the PERMANENT, ONLY
    # topology. The V1 (18-node legacy) graph and the AGENT_V2_REACT_ENABLED /
    # AGENT_V3_* feature flags were removed; the ReAct ("V3") behavior is
    # unconditional. AGENT_LLM_MODEL is the model ID (not a feature flag) and
    # keeps a fail-closed empty-string safety net in llm_client, now
    # unreachable via the default below.
    # Max iterations per turn (REQ-AGENT-LOOP-ITERATION-001).
    AGENT_MAX_ITERATIONS: int = 6
    # Per-turn cumulative LLM token budget (REQ-AGENT-PERF-TURN-BUDGET-001).
    AGENT_TURN_TOKEN_BUDGET: int = 32000
    # Per-tool dispatch timeout in seconds (REQ-AGENT-FAILURE-TOOL-001).
    AGENT_TOOL_TIMEOUT_S: float = 5.0
    # LLM model ID for the ReAct agent. Defaults to the production model so the
    # system works with no env override. Empty string is still honored as a
    # fail-closed safety net in llm_client (unreachable via this default).
    AGENT_LLM_MODEL: str = "nova-lite"
    # Per-LLM-call timeout in seconds.
    AGENT_LLM_TIMEOUT_S: float = 5.0
    # Transient-error retry knobs (SPEC-AGENT-V2-REACT runtime hardening).
    # Bedrock Nova bursts get throttled → LiteLLM surfaces HTTP 500; a short
    # retry survives the throttle without consuming a ReAct iteration.
    AGENT_LLM_MAX_RETRIES: int = 2
    AGENT_TOOL_MAX_RETRIES: int = 1
    # Dedicated wall-clock budget for the TERMINAL `respond` dispatch. It sends
    # the text + up to 12 product cards sequentially (~1-1.7s each via Telegram
    # send_card) → 12-20s, which structurally exceeds AGENT_TOOL_TIMEOUT_S (5s).
    # `respond` is never retried (side-effecting + terminal), so a generous
    # bound here just prevents a spurious mid-carousel TimeoutError from
    # truncating a legit card batch (SPEC-AGENT-V2-REACT double-send fix).
    AGENT_RESPOND_TIMEOUT_S: float = 30.0

    # SPEC-AGENT-V2-CLEANUP-001 — the four V3 enhancements (memory injection,
    # Reflexion loop, proactive suggestion, cross-thread dislike memory) are now
    # UNCONDITIONAL. The AGENT_V3_*_ENABLED flags were removed; only the
    # memory-injection payload token cap remains as a tunable.
    # Memory-injection payload token cap (char-approx ×4).
    # Bumped 1500→3000 (2026-05-20) to widen the recent-turn digest so the
    # ReAct agent has more room to reason about prior search/refine signals
    # and recent user phrasing without exhausting the budget after 2-3 turns.
    AGENT_V3_MEMORY_MAX_TOKENS: int = 3000

    # SPEC-CHAT-STATE-REDIS-001 / REQ-CHAT-STATE-004 — single gate for 3환경
    # (local docker-compose / fakeredis pytest / dev-ai prod DB 1). Default
    # points at the local docker-compose redis. Prod overrides via env
    # `REDIS_URL=redis://:${REDIS_AUTH}@redis:6379/1` (Langfuse uses DB 0).
    REDIS_URL: str = "redis://localhost:6379/1"

    @property
    def self_critique_fastpath_drop_filters(self) -> list[str]:
        return [s.strip().lower() for s in self.SELF_CRITIQUE_FASTPATH_DROP_FILTERS.split(",") if s.strip()]

    @property
    def ask_clarify_ambiguous_labels(self) -> list[str]:
        return [s.strip().lower() for s in self.ASK_CLARIFY_AMBIGUOUS_LABELS.split(",") if s.strip()]

    @property
    def ask_clarify_ambiguous_subcategories(self) -> list[str]:
        return [s.strip().lower() for s in self.ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES.split(",") if s.strip()]

    @property
    def allowed_image_hosts(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_IMAGE_HOSTS.split(",") if h.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
