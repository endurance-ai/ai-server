from functools import lru_cache
from typing import Literal

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
    # SPEC-MODAL-KEEP-WARM-001 — 배경 태스크로 Modal `/health` 를 주기적으로
    # ping 해 컨테이너를 warm 유지. Modal 이 `min_containers=0` (scale-to-zero)
    # 로 배포돼 있어 idle 후 첫 요청이 ~19-20s 콜드 스타트를 겪는 것에 대한
    # 실용적 완화책. 근본은 Modal 소스에 `min_containers=1` 지정이지만
    # 소스 접근이 워크스페이스 이전 대기 중이라 그동안의 우회.
    MODAL_KEEP_WARM_ENABLED: bool = True
    # Ping 주기 (초). Modal `scaledown_window` 기본값 5분 (300초) 보다 확실히
    # 짧아야 warm 유지됨. 실측에서 다른 콜드 컨테이너 히트 사례도 있어
    # 안전 마진 확보 위해 3분 (180초) 을 기본으로. 최소 30초로 클램프.
    MODAL_KEEP_WARM_INTERVAL_S: int = 180
    # 단일 ping 타임아웃. `check_connection` 은 GET /health 라 정상 시 <1초.
    # 콜드 컨테이너 히트 시 5-15초까지 걸릴 수 있어 25초로 여유. 이걸 초과
    # 하면 조용히 다음 iteration 대기 (재시도 안 함 — 다음 주기 때 다시 시도).
    MODAL_KEEP_WARM_TIMEOUT_S: float = 25.0

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
    # 2026-07-15 정밀 필터 (백엔드 products.subcategory/color 정규화 연동).
    # v6 의 p_subcategory/p_color_family 는 EXACT·무완화 필터 — kill-switch 로
    # 즉시 회귀 가능하도록 개별 플래그. RELAX_MIN 미만이면 필터 제거 재시도.
    SEARCH_SUBCATEGORY_FILTER_ENABLED: bool = True
    SEARCH_COLOR_FILTER_ENABLED: bool = True
    # 2026-08-16 — 5 → 20. 결과가 20개 미만이면 subcat/color 를 풀어 유사도로
    # 채운다(정밀매치 먼저, 완화분 뒤 dedup). 2열 그리드가 빈약해 보이지 않게
    # 리콜 보강 — 상한은 SEARCH_DEFAULT_K(50).
    SEARCH_FILTER_RELAX_MIN: int = 20
    # 2026-07-16 — p_gender 상품 레벨 하드 필터 (gender[] && [g,'unisex']).
    # 시맨틱 제약이라 완화 재시도 대상 아님. kill-switch.
    SEARCH_GENDER_FILTER_ENABLED: bool = True
    # SPEC-SEARCH-HYBRID-001 — 텍스트 쿼리 하이브리드(이미지⊕텍스트 임베딩) 랭킹.
    # 텍스트 쿼리를 상품 이미지 임베딩에만 매칭하면 CLIP 계열 modality gap 으로
    # distance 가 뭉쳐(0.86±0.015) 랭킹 신호가 흐리다. product_features.text_embedding
    # (동일 FashionSigLIP 텍스트 임베딩)과의 text↔text 거리를 블렌드하면 색/소재/패턴
    # attribute precision@15 이 골든셋 172쿼리에서 0.70→0.81 로 오른다(w_text=0.3 이
    # best-of-both — 순수 텍스트 교체는 소재/패턴 회귀라 폐기). search_products_hybrid_v1
    # RPC 사용. 순수 텍스트 경로 전용(사진/blended/pinned-anchor 는 이미지 벡터라 제외).
    # kill-switch: off → 기존 v6 이미지 단독 경로로 즉시 byte-identical 회귀.
    SEARCH_HYBRID_ENABLED: bool = False
    SEARCH_HYBRID_W_TEXT: float = 0.3  # 블렌드 텍스트 가중 (0=이미지 단독, 1=텍스트 단독)
    SEARCH_HYBRID_POOL: int = 100  # 공간별 kNN 풀 크기 (union 후보 상한 ~2*pool)
    # 다양성 cap — 2026-06-17 완화 (이전: brand=2, platform=3).
    # Langfuse 트레이스 분석에서 raw 50개 후보 중 45개가 cap 으로 잘려
    # 5개만 남는 케이스 다수 발견. 특히 platform cap 은 "어디서 파느냐" 만
    # 통제할 뿐 옷의 본질과 무관 → 정확도만 깎고 다양성 이득은 미미.
    # brand_cap=5: 첫 album(5장) 이 단일 브랜드여도 OK (진짜 매치면 그게 정답).
    # 15개 final 안엔 최소 3 brand 보장 (15/5) — 모노컬쳐 완전 차단까진 아니어도
    # "ZARA 광고같다" 까진 안 감.
    SEARCH_BRAND_CAP: int = 5  # 브랜드당 최대 (첫 album 단일 브랜드 허용)
    SEARCH_PLATFORM_CAP: int = 8  # 플랫폼당 최대 (사실상 비활성)
    # SPEC-DIVERSIFY-ATTR-CAP — attribute-level diversity caps applied
    # alongside brand/platform. Keyed off `brand_nodes.attributes` first
    # token (vibe[0] / silhouette[0]) — lookup is via brand_node_cache.
    # Cache miss → cap skipped for that candidate (fail-open). Cap > 0
    # enables, 0 disables that dimension. Final_limit is typically 15,
    # so 5 forces ≥3 distinct groups per page without starving results.
    SEARCH_VIBE_CAP: int = 5
    SEARCH_SILHOUETTE_CAP: int = 5

    # SPEC-BRAND-2TOWER-RESCORE — blend brand_multimodal_embeddings into
    # the product-distance ranking. α weights product distance vs brand
    # distance; α=1.0 disables (equivalent to baseline). Default 0.8 keeps
    # product visual signal dominant but lets brand identity break ties on
    # close matches.
    BRAND_2TOWER_ENABLED: bool = True
    BRAND_2TOWER_ALPHA: float = 0.8
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

    # 메인 큐레이션 (GET /v1/curation) — auto 구좌 refresher.
    # 구좌 메타데이터와 editorial 상품 목록은 어드민 페이지가 소유한다.
    CURATION_REFRESH_ENABLED: bool = True
    CURATION_REFRESH_INTERVAL_S: int = 900
    CURATION_SEASON: Literal["summer", "winter"] = "summer"

    # 알림 1차 — 재입고 / 가격 하락 / 관심 브랜드 신규 상품 (scripts/notify_batch.py)
    # 가격 하락 임계치. 찜한 시점(또는 마지막 알림 시점) 기준가 대비 비율.
    NOTIFY_PRICE_DROP_THRESHOLD: float = 0.15
    # 브랜드 신규 상품 알림은 주 N일까지만 (스팸 방지 — 실측 ≈400 유저-상품 쌍/일).
    NOTIFY_BRAND_NEW_WEEKLY_CAP: int = 3
    # 한 다이제스트에 담는 브랜드 신규 상품 최대 개수. 초과분은 이벤트로 기록하지
    # 않아 보존창 안에서 다음 발송 가능일 후보로 다시 나온다.
    NOTIFY_BRAND_NEW_MAX_ITEMS: int = 5
    # 신규 상품 보존창(일). 매 회차 이 창을 훑고 이미 알림 나간 (user, product) 만
    # 제외한다 — 워터마크가 아니다. 성별은 VLM 배치가 채우므로 신상은 항상 잠시
    # 성별 미해결 상태를 거친다. 창은 VLM 주기보다 넉넉히 길어야 그 사이에 놓치지
    # 않는다 (VLM 일배치 기준 14일이면 한참 여유).
    NOTIFY_NEW_PRODUCT_WINDOW_D: int = 14
    # 브랜드 세일 알림 임계값. sale_price < original_price 인 상품 비율이 이 값을
    # 처음 넘는 순간에만 팔로워에게 알린다. sale_price 는 할인 시에만 채워지므로
    # (실측 커버리지 ≈5%) 30% 는 "브랜드 전면 세일" 수준의 강한 신호를 요구한다.
    NOTIFY_BRAND_SALE_THRESHOLD: float = 0.30
    # 브랜드 세일 푸시는 주 N일까지만. brand_new 캡(NOTIFY_BRAND_NEW_WEEKLY_CAP)과
    # 독립적으로 센다 — 전용 카테고리(brand_sale_digest)로 집계하므로 두 종류의
    # 발송이 서로의 주간 캡을 잠식하지 않는다. 인박스 적재는 이 캡과 무관하게 항상
    # 이뤄지고, 캡은 APNs 푸시 발송에만 적용된다.
    NOTIFY_BRAND_SALE_WEEKLY_CAP: int = 3
    # 세일 비율 집계에서 제외할 최소 카탈로그 크기. 감지가 팔로워 있는 브랜드만
    # 훑던 시절엔 필요 없었지만(팔로우된 브랜드는 실카탈로그가 있다), 브랜드 소식을
    # 정본으로 남기려고 전체 브랜드를 스캔하면서 필요해졌다 — 상품 2개 중 1개가
    # 할인이면 비율 50% 로 임계값을 넘어 브랜드 홈에 가짜 "전면 세일" 이 걸린다.
    NOTIFY_BRAND_SALE_MIN_PRODUCTS: int = 10
    # 브랜드 홈 "신상 N개" 요약이 세는 기간. 알림용 보존창
    # (NOTIFY_NEW_PRODUCT_WINDOW_D=14)과 별개다 — 그쪽은 "캡에 걸려 아직 못 보낸 걸
    # 다시 후보로 올리는" 재시도 창이고, 이건 화면에 보여줄 "최근" 의 길이다.
    NOTIFY_BRAND_NEW_SUMMARY_WINDOW_D: int = 7
    # 하루 몫(NOTIFY_BRAND_NEW_MAX_ITEMS) 안에서 한 브랜드가 가져갈 수 있는 최대치.
    # products.created_at 은 출시일이 아니라 적재 시각이고 크롤은 브랜드 단위로 통째
    # 도는데(실측: 한 브랜드 775건이 2초 안에 적재), 그대로 최신순 5개를 뽑으면
    # "마지막에 크롤된 브랜드" 가 하루를 독식한다. 슬롯이 남으면 초과분으로 채우므로
    # 브랜드를 하나만 팔로우한 유저의 물량은 줄지 않는다.
    NOTIFY_BRAND_NEW_MAX_PER_BRAND: int = 2
    # 브랜드를 처음 수집하면 카탈로그 전체가 같은 배치로 들어와 전부 "신상" 이 된다
    # (실측: 최근 14일 후보 15,438건 중 1,992건 = 13%). 브랜드 최초 적재 후 이 시간
    # 안에 들어온 행은 신상으로 치지 않는다.
    NOTIFY_BRAND_ONBOARDING_GRACE_H: int = 24

    # Standalone worker. The process exits without doing work unless enabled.
    NOTIFICATION_WORKER_ENABLED: bool = False
    NOTIFY_TIMEZONE: str = "Asia/Seoul"
    NOTIFY_QUIET_START_HOUR: int = 22
    NOTIFY_QUIET_END_HOUR: int = 8
    NOTIFY_SAVED_SCAN_TIME: str = "09:00"
    NOTIFY_SAVED_SEND_TIME: str = "09:30"
    NOTIFY_BRAND_SCAN_TIME: str = "10:30"
    NOTIFY_BRAND_SEND_TIME: str = "11:00"
    NOTIFY_WORKER_POLL_S: int = 30
    NOTIFY_DELIVERY_BATCH_SIZE: int = 100
    NOTIFY_APNS_CONCURRENCY: int = 20
    NOTIFY_MAX_DELIVERY_ATTEMPTS: int = 5
    # 한 사이클에서 아웃박스를 비울 때까지 deliver_pending 을 반복하되 이 상한에서 멈춘다.
    # 상한이 없으면 재시도가 계속 due 로 돌아오는 상황에서 사이클이 끝나지 않는다.
    # BATCH_SIZE(=클레임 단위)와 다르다 — 이쪽은 사이클 전체 예산이다.
    NOTIFY_DELIVERY_MAX_PER_CYCLE: int = 5000
    # 아웃박스 적재를 유저 N명씩 끊어 커밋한다. 전체를 한 트랜잭션으로 묶으면
    # 유저·카테고리마다 잡는 pg_advisory_xact_lock 이 커밋까지 누적돼
    # 공유 락 테이블(max_locks_per_transaction × max_connections)을 고갈시킨다.
    NOTIFY_OUTBOX_CHUNK_USERS: int = 200

    # 보존 정책. 되돌릴 수 없는 삭제라 **기본 비활성**이다 — 운영에서 먼저
    # `scripts/notify_retention.py --dry-run` 으로 삭제 대상 규모를 확인한 뒤 켠다.
    NOTIFY_RETENTION_ENABLED: bool = False
    # 알림함이 보여주는 기간. 피드 쿼리(app/api/notifications.py)에는 날짜 창이 없어
    # keyset 으로 무한히 거슬러 올라갈 수 있으므로, 이 값이 사실상 그 상한이 된다.
    NOTIFY_RETENTION_FEED_D: int = 180
    NOTIFY_RETENTION_SCAN_TIME: str = "04:00"
    # 한 트랜잭션에서 지울 행 수. 아웃박스 청킹과 같은 이유로 끊는다.
    NOTIFY_RETENTION_BATCH: int = 5000

    # APNs — environment-specific topic keys. Base64 avoids multiline .env
    # parsing mistakes. Legacy APNS_* values remain a production fallback.
    APNS_TOPIC: str = "com.kikoai.app"
    ANDROID_PACKAGE: str = "com.kikoai.app"
    APNS_SANDBOX_AUTH_KEY_B64: str = ""
    APNS_SANDBOX_AUTH_KEY_PATH: str = ""
    APNS_SANDBOX_KEY_ID: str = ""
    APNS_PRODUCTION_AUTH_KEY_B64: str = ""
    APNS_PRODUCTION_AUTH_KEY_PATH: str = ""
    APNS_PRODUCTION_KEY_ID: str = ""
    APNS_AUTH_KEY: str = ""  # .p8 PEM 본문 (개행 포함). 파일 대신 env 로 넣을 때.
    APNS_AUTH_KEY_PATH: str = ""  # 또는 .p8 파일 경로. 둘 다 있으면 본문 우선.
    APNS_KEY_ID: str = ""
    APNS_TEAM_ID: str = ""
    APNS_BUNDLE_ID: str = ""  # legacy alias; APNS_TOPIC is authoritative
    APNS_USE_SANDBOX: bool = False
    APNS_TIMEOUT_S: float = 10.0

    # 소비자 소셜 로그인 (SPEC-AUTH-SOCIAL-001)
    GOOGLE_CLIENT_ID: str = ""  # Google Cloud Console > OAuth 2.0 (iOS type)
    APPLE_CLIENT_ID: str = ""  # iOS Bundle ID (com.kikoai.app)
    JWT_SECRET: str = ""  # HS256 signing key (32+ chars). Empty = dev skip.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # User image uploads — S3 presigned PUT + CloudFront final URL.
    UPLOADS_S3_BUCKET: str = ""
    UPLOADS_S3_REGION: str = "ap-northeast-2"
    UPLOADS_S3_ACCESS_KEY_ID: str = ""
    UPLOADS_S3_SECRET_ACCESS_KEY: str = ""
    UPLOADS_S3_SESSION_TOKEN: str = ""
    UPLOADS_PUBLIC_BASE_URL: str = ""
    UPLOADS_KEY_PREFIX: str = "uploads"
    UPLOADS_MAX_SIZE_BYTES: int = 10_485_760
    UPLOADS_PRESIGN_EXPIRES_SECONDS: int = 300
    # Standard AWS env fallback for upload signing.
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_SESSION_TOKEN: str = ""

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

    # Discord signup notification webhook — empty = feature off (silent no-op).
    # Named specifically (not DISCORD_WEBHOOK_URL, which deploy.ai.sh reserves)
    # so multiple Discord webhooks can coexist.
    DISCORD_SIGNUP_WEBHOOK_URL: str = ""
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

    # SPEC-PERSONALIZE-RERANK — score-based re-ordering of v6 RPC raw rows
    # using TasteProfile × brand_nodes.attributes (vibe / price_tier /
    # gender_lean). Runs between search_step and diversify_step. Pure
    # function; fail-open (no profile / no user_key / empty cache → no-op).
    # Default weights are conservative — `1 - distance` typically sits in
    # 0.6~0.8, so a +0.10 liked-brand bump shifts ordering for tied/close
    # candidates without overpowering the embedding signal. All knobs are
    # env-tunable so the live A/B can sweep them without code changes.
    # 2026-08-03 (SPEC-SEARCH-HYBRID-001) — relevance-priority 하향. 골든셋 격리
    # 측정: 재정렬 feature 항이 top-5 attribute precision 을 평균 −0.085(소재 −0.17)
    # 낮춘다. base 후보 간 거리 격차(하이브리드 블렌드 스프레드 ~0.05~0.08, v6 이미지
    # ~0.015)보다 가중이 커 relevance 를 뒤엎기 때문. "취향보다 검색이 먼저" 정책에
    # 맞춰 지배적 항(feature/liked/disliked)을 ~절반으로 낮춰 tie-breaker 로 강등한다.
    # 전부 env-tunable — 라이브 A/B 로 재상향 가능. (이전: 0.10/0.20/0.15)
    PERSONALIZE_RERANK_ENABLED: bool = True
    PERSONALIZE_LIKED_BRAND_W: float = 0.05
    PERSONALIZE_DISLIKED_BRAND_W: float = 0.10
    PERSONALIZE_KEYWORD_W: float = 0.02
    PERSONALIZE_PRICE_FIT_W: float = 0.05
    PERSONALIZE_GENDER_MISMATCH_W: float = 0.10
    PERSONALIZE_FEATURE_W: float = 0.06

    # 속성정렬 rerank ("우와 비슷하다") — 쿼리가 명시한 fit/material 을 후보
    # product_features.feature_metadata 와 정렬해 가산. color/subcategory 는 이미
    # 하드게이트라 제외. 개인화와 달리 쿼리 의도라 익명/콜드 유저에도 적용된다.
    # 골든셋 실측(pool60→top20): fit_feat 0.38→0.83, color_feat 0.52→0.70.
    ATTR_ALIGN_ENABLED: bool = True
    ATTR_ALIGN_FIT_W: float = 0.20
    ATTR_ALIGN_MATERIAL_W: float = 0.08
    # 색 필터가 재고 부족으로 relax(하드게이트 해제)됐을 때, 요청 색을 소프트
    # 부스트로 돌려 exact-color(예: 아크네 블랙 진)를 상단에 유지하고 유사상품이
    # 아래를 채우게 한다. relax 안 됐으면(게이트 유지) color 는 부스트 대상 아님.
    ATTR_ALIGN_COLOR_W: float = 0.25

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

    # UX 정책 (2026-06-04): sendMediaGroup 5장 묶음 대신 카드를 개별로 전송.
    # 각 카드가 자체 인라인 키보드(♥ / 구매)를 가지므로 사용자 액션이 더 자연스러움.
    # True → 개별 전송 (운영 권장), False → 기존 album 동작 (롤백용).
    INDIVIDUAL_CARD_DELIVERY: bool = True
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
    # 32k → 80k (2026-07-06): the 32k catch-all cap was calibrated when the
    # ReAct prompt was ~5-6k/iter (SPEC-AGENT-V2-REACT plan.md rationale).
    # Since then _STATIC_SYSTEM_PROMPT, _PROACTIVE_DIRECTIVE, tool schema,
    # and memory-context injection grew each iter to ~11k tokens. Traces on
    # 2026-07-06 (e.g. 427418c3 "카프리 팬츠 더 비슷하게") showed the cap
    # firing at iter 4 before the LLM could call `respond` — search
    # succeeded (8 candidates ready) but the user saw nothing. 80k lets
    # max_iter=6 complete at 13k/iter without tripping the guard, while
    # still catching runaways in the 100k+ range.
    AGENT_TURN_TOKEN_BUDGET: int = 80000
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

    # Beta CTR — base URL for the outbound redirect proxy (`/r/{token}`).
    # When empty, respond.send_hybrid_batch falls back to raw product URLs
    # (feature OFF). Set to the public host of this AI server in prod.
    # Example: `PUBLIC_BASE_URL=https://ai.kiko.fashion`
    PUBLIC_BASE_URL: str = ""

    # 260611 — cap fake-door 멤버십 랜딩 페이지. `/m/membership` redirect 가
    # 302 로 보내는 목적지. 비어 있을 때 `https://kikoai.me/price` 로 폴백.
    MEMBERSHIP_LANDING_URL: str = "https://kikoai.me/price"

    # SPEC-DAILY-TOKEN-CAP-001 — per-user daily token cap (resets at KST midnight).
    # Deploy with DAILY_TOKEN_CAP_ENABLED=false first to collect Langfuse baseline.
    # After 1-2 weeks: set CAP_TIER_FREE = p95_daily_tokens * 1.2, then enable.
    DAILY_TOKEN_CAP_ENABLED: bool = True
    # DAILY_TOKEN_CAP: deprecated alias for CAP_TIER_FREE. If set explicitly via
    # env var, it overrides CAP_TIER_FREE for the free tier. Use CAP_TIER_FREE directly.
    DAILY_TOKEN_CAP: int = 200_000
    # Per-tier daily caps (tokens). 0 = unlimited.
    # CAP_TIER_FREE takes precedence over DAILY_TOKEN_CAP when both are set.
    # CAP_TIER_FREE: int = 200_000
    CAP_TIER_FREE: int = 500_000
    CAP_TIER_STANDARD: int = 500_000
    CAP_TIER_PRO: int = 1_000_000
    CAP_TIER_DEVELOPER: int = 0  # unlimited — for devs and internal testing

    @property
    def effective_free_cap(self) -> int:
        """Free-tier cap, giving CAP_TIER_FREE precedence over DAILY_TOKEN_CAP.

        If both differ from their defaults, CAP_TIER_FREE wins.
        DAILY_TOKEN_CAP is preserved only when CAP_TIER_FREE is still at its
        default (200_000), meaning the operator hasn't migrated yet.
        """
        if self.CAP_TIER_FREE != 200_000:
            return self.CAP_TIER_FREE
        return self.DAILY_TOKEN_CAP

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
