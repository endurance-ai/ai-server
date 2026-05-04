from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    PROJECT_NAME: str = "portal-ai-server"
    VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False

    # Supabase (검색 RPC + 상품 메타 조회)
    SUPABASE_URL: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

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

    # 검색 파라미터 기본값
    SEARCH_DEFAULT_K: int = 50  # RPC top-k
    SEARCH_BRAND_CAP: int = 2  # 다양성: 브랜드당 최대
    SEARCH_PLATFORM_CAP: int = 3  # 다양성: 플랫폼당 최대
    SEARCH_FINAL_LIMIT: int = 15  # 최종 응답 개수

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
    VISION_MODEL: str = "gpt-4o-mini"
    BOT_LANGUAGE: str = "en"
    SESSION_TTL_SECONDS: int = 1800

    @property
    def allowed_image_hosts(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_IMAGE_HOSTS.split(",") if h.strip()]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
