"""SPEC-AGENT-V2-REACT / OQ-1 + OQ-2 — Bedrock (nova-lite) compatibility.

`get_llm()` must build a tool-bound ChatOpenAI WITHOUT forcing `tool_choice`
(Bedrock via LiteLLM rejects `tool_choice` with HTTP 400). These are
mock-level assertions — no network call.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def _reset_singleton():
    import app.agents.llm_client as lc

    lc._llm = None
    yield
    lc._llm = None


def test_get_llm_does_not_force_tool_choice(monkeypatch, _reset_singleton):
    """bind_tools must be called with tool_choice=None (never forced)."""
    import app.agents.llm_client as lc
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "nova-lite", raising=False)

    bound_sentinel = object()
    fake_client = MagicMock()
    fake_client.bind_tools.return_value = bound_sentinel
    fake_ctor = MagicMock(return_value=fake_client)

    fake_module = MagicMock()
    fake_module.ChatOpenAI = fake_ctor
    monkeypatch.setitem(__import__("sys").modules, "langchain_openai", fake_module)

    result = lc.get_llm()

    assert result is bound_sentinel
    # ChatOpenAI constructed once with the configured model.
    ctor_kwargs = fake_ctor.call_args.kwargs
    assert ctor_kwargs["model"] == "nova-lite"
    # drop_params instructs LiteLLM to strip provider-unsupported params
    # (e.g. tool_choice) instead of returning HTTP 400 for Bedrock.
    assert ctor_kwargs.get("extra_body") == {"drop_params": True}

    # bind_tools must NOT force a tool_choice. Either it is absent, or it is
    # explicitly None — never a truthy string/dict that langchain would inject.
    bind_args = fake_client.bind_tools.call_args
    tool_choice = bind_args.kwargs.get("tool_choice", None)
    assert tool_choice is None
    # Tools schema is still passed (first positional arg, non-empty list).
    # SPEC-AGENT-V2-CLEANUP-001 — 8 tools (suggest_next_step unconditional).
    tools_schema = bind_args.args[0]
    assert isinstance(tools_schema, list) and len(tools_schema) == 8


def test_get_llm_fail_closed_when_model_empty(monkeypatch, _reset_singleton):
    """Empty AGENT_LLM_MODEL → fail-closed (returns None, no construction)."""
    import app.agents.llm_client as lc
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "", raising=False)

    assert lc.get_llm() is None
