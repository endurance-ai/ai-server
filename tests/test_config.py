from app.core.config import Settings


def test_settings_defaults():
    """기본값으로 Settings 생성 가능해야 한다."""
    s = Settings()
    assert s.PROJECT_NAME == "portal-ai-server"
    assert s.ENVIRONMENT == "development"
    assert s.LITELLM_BASE_URL == "http://localhost:4000"
    assert s.SEARCH_DEFAULT_K == 50
    assert s.SEARCH_BRAND_CAP == 2
    assert s.SEARCH_PLATFORM_CAP == 3


def test_settings_overrides():
    """env 값 override 가능해야 한다."""
    s = Settings(
        SUPABASE_URL="https://test.supabase.co",
        MODAL_EMBED_URL="https://test.modal.run",
        LANGFUSE_HOST="http://langfuse:3000",
    )
    assert s.SUPABASE_URL == "https://test.supabase.co"
    assert s.MODAL_EMBED_URL == "https://test.modal.run"
    assert s.LANGFUSE_HOST == "http://langfuse:3000"
