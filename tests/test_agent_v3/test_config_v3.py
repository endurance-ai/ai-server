"""SPEC-AGENT-V3-REACT / T1 — 5 new env vars.

REQ-AGENT-V3-MEM-CAP-001 (env) + the 4 sub-flag FLAG REQ preconditions.
Regression: with nothing set, all 4 sub-flags default false and the
char-cap defaults to 1500 (byte-identical-off precondition).
"""

from __future__ import annotations

from app.core.config import Settings


def test_v3_subflags_default_false():
    """All 4 V3 sub-flags default false (REQ-AGENT-V3-COMPAT-ALLOFF-001 precondition)."""
    s = Settings(_env_file=None)
    assert s.AGENT_V3_MEMORY_INJECTION_ENABLED is False
    assert s.AGENT_V3_REFLEXION_ENABLED is False
    assert s.AGENT_V3_PROACTIVE_ENABLED is False
    assert s.AGENT_V3_DISLIKE_MEMORY_ENABLED is False


def test_v3_memory_max_tokens_default():
    """REQ-AGENT-V3-MEM-CAP-001 — default char-approx token cap is 1500."""
    s = Settings(_env_file=None)
    assert s.AGENT_V3_MEMORY_MAX_TOKENS == 1500


def test_v3_subflags_independently_togglable():
    """Each sub-flag flips independently — no coupling."""
    s = Settings(
        _env_file=None,
        AGENT_V3_MEMORY_INJECTION_ENABLED=True,
        AGENT_V3_DISLIKE_MEMORY_ENABLED=True,
    )
    assert s.AGENT_V3_MEMORY_INJECTION_ENABLED is True
    assert s.AGENT_V3_REFLEXION_ENABLED is False
    assert s.AGENT_V3_PROACTIVE_ENABLED is False
    assert s.AGENT_V3_DISLIKE_MEMORY_ENABLED is True


def test_v3_memory_max_tokens_override():
    s = Settings(_env_file=None, AGENT_V3_MEMORY_MAX_TOKENS=200)
    assert s.AGENT_V3_MEMORY_MAX_TOKENS == 200


def test_v3_master_gate_default_unchanged():
    """V3 envs are additive — the V2 master gate keeps its prior default."""
    s = Settings(_env_file=None)
    assert s.AGENT_V2_REACT_ENABLED is False
    assert s.AGENT_LLM_MODEL == ""
