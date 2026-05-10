from app.core.config import Settings


def test_settings_defaults():
    """기본값으로 Settings 생성 가능해야 한다. .env 영향 배제 (_env_file=None)."""
    s = Settings(_env_file=None)
    assert s.PROJECT_NAME == "portal-ai-server"
    assert s.ENVIRONMENT == "development"
    assert s.LITELLM_BASE_URL == "http://localhost:4000"
    assert s.SEARCH_DEFAULT_K == 50
    assert s.SEARCH_BRAND_CAP == 2
    assert s.SEARCH_PLATFORM_CAP == 3


def test_settings_overrides():
    """env 값 override 가능해야 한다."""
    s = Settings(
        DB_URL="http://test.local",
        MODAL_EMBED_URL="https://test.modal.run",
        LANGFUSE_HOST="http://langfuse:3000",
    )
    assert s.DB_URL == "http://test.local"
    assert s.MODAL_EMBED_URL == "https://test.modal.run"
    assert s.LANGFUSE_HOST == "http://langfuse:3000"


# ── SPEC-AGENT-001: 새 env 변수 + 의존성 import 가능 여부 검증 ──────────────


def test_agent_response_settings_defaults():
    """REQ-LLM-003: respond/ask_clarify 노드가 사용할 env 기본값."""
    s = Settings(_env_file=None)
    assert s.RESPONSE_MODEL == "gpt-4o-mini"
    assert s.RESPONSE_TIMEOUT_MS == 5000
    assert s.RESPONSE_MAX_TOKENS == 200
    assert s.ASK_CLARIFY_MIN_DESC_TOKENS == 3
    assert s.ask_clarify_ambiguous_labels == ["item", "clothing", "thing", "piece"]


def test_langgraph_and_langchain_imports_succeed():
    """REQ-AGENT-001/002: 새 의존성이 import 가능해야 한다."""
    from langchain_core.messages import BaseMessage  # noqa: F401
    from langchain_openai import ChatOpenAI  # noqa: F401
    from langgraph.graph import END, START, StateGraph  # noqa: F401
    from langgraph.graph.message import add_messages  # noqa: F401
