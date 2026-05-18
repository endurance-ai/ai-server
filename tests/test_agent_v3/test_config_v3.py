"""SPEC-AGENT-V2-CLEANUP-001 — config after the flag removal.

The AGENT_V2_REACT_ENABLED / AGENT_V3_*_ENABLED feature flags were deleted
(the ReAct agent + the four V3 enhancements are now unconditional). The
remaining V3 tunable is AGENT_V3_MEMORY_MAX_TOKENS, and the model defaults
moved to nova-lite so the system works with no env override.
"""

from __future__ import annotations

from app.core.config import Settings


def test_v3_flags_removed():
    """The 5 removed feature flags must no longer exist on Settings."""
    s = Settings(_env_file=None)
    for removed in (
        "AGENT_V2_REACT_ENABLED",
        "AGENT_V3_MEMORY_INJECTION_ENABLED",
        "AGENT_V3_REFLEXION_ENABLED",
        "AGENT_V3_PROACTIVE_ENABLED",
        "AGENT_V3_DISLIKE_MEMORY_ENABLED",
    ):
        assert not hasattr(s, removed), f"{removed} should have been removed"


def test_v3_memory_max_tokens_default():
    """The char-approx token cap default is 1500."""
    s = Settings(_env_file=None)
    assert s.AGENT_V3_MEMORY_MAX_TOKENS == 1500


def test_v3_memory_max_tokens_override():
    s = Settings(_env_file=None, AGENT_V3_MEMORY_MAX_TOKENS=200)
    assert s.AGENT_V3_MEMORY_MAX_TOKENS == 200


def test_model_defaults_are_nova_lite():
    """SPEC-AGENT-V2-CLEANUP-001 — defaults work with no env override."""
    s = Settings(_env_file=None)
    assert s.AGENT_LLM_MODEL == "nova-lite"
    assert s.EVALUATOR_MODEL == "nova-lite"
