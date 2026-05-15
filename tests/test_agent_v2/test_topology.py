"""SPEC-AGENT-V2-REACT / REQ-AGENT-COMPAT-FLAG-001 + REQ-AGENT-TOPOLOGY-SUPERSEDE-001.

Topology gating via feature flag + AGENT_LLM_MODEL.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def _v1_settings(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_V2_REACT_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "", raising=False)
    return cfg.settings


@pytest.fixture
def _v2_settings(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_V2_REACT_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "gpt-4o-mini", raising=False)
    return cfg.settings


def test_v1_topology_when_flag_false(_v1_settings):
    import app.graphs.fashion_bot as fb

    importlib.reload(fb)
    nodes = fb.build_graph().get_graph().nodes
    # V1 includes the deprecated set.
    assert "respond" in nodes
    assert "critique_apply" in nodes
    assert "taste_update" in nodes


def test_v2_topology_when_flag_true(_v2_settings):
    import app.graphs.fashion_bot as fb

    importlib.reload(fb)
    nodes = fb.build_graph().get_graph().nodes
    assert "agent" in nodes
    # V2 excludes the deprecated set.
    assert "respond" not in nodes
    assert "critique_apply" not in nodes
    assert "taste_update" not in nodes
    assert "router_text" not in nodes
    assert "evaluator" not in nodes
    # Onboarding subgraph preserved.
    assert "onboard_intro" in nodes
    assert "onboard_mood" in nodes
    assert "onboard_color" in nodes
    assert "onboard_fit" in nodes
    assert "onboard_pinterest" in nodes


def test_v2_fail_closed_when_model_unset(monkeypatch):
    """AGENT_V2_REACT_ENABLED=true but AGENT_LLM_MODEL empty → V1 topology."""
    from app.core import config as cfg

    monkeypatch.setattr(cfg.settings, "AGENT_V2_REACT_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg.settings, "AGENT_LLM_MODEL", "", raising=False)
    import app.graphs.fashion_bot as fb

    importlib.reload(fb)
    nodes = fb.build_graph().get_graph().nodes
    assert "agent" not in nodes  # V1 path
    assert "respond" in nodes
