"""SPEC-AGENT-V2-CLEANUP-001 — config after the flag removal.

The AGENT_V2_REACT_ENABLED / AGENT_V3_*_ENABLED feature flags were deleted
(the ReAct agent + the four V3 enhancements are now unconditional). The
remaining V3 tunable is AGENT_V3_MEMORY_MAX_TOKENS, and the model defaults
moved to nova-lite so the system works with no env override.
"""

from __future__ import annotations

from app.core.config import Settings


def test_v3_flags_removed():
    """The removed feature flags must no longer exist on Settings."""
    s = Settings(_env_file=None)
    for removed in (
        "AGENT_V2_REACT_ENABLED",
        "AGENT_V3_MEMORY_INJECTION_ENABLED",
        "AGENT_V3_REFLEXION_ENABLED",
        "AGENT_V3_PROACTIVE_ENABLED",
        "AGENT_V3_DISLIKE_MEMORY_ENABLED",
        "SELF_CRITIQUE_ENABLED",
        "SELF_CRITIQUE_MAX_ITERATIONS",
        "SELF_CRITIQUE_THRESHOLD",
        "SELF_CRITIQUE_TIMEOUT_S",
        "SELF_CRITIQUE_FASTPATH_DROP_FILTERS",
        "EVALUATOR_MODEL",
        "EVALUATOR_MAX_TOKENS",
        "EVALUATOR_TEMPERATURE",
        "EVALUATOR_TIMEOUT_S",
        "CLARIFY_CARDS_ENABLED",
        "VISION_SCHEMA_V2",
        "ENHANCE_QUERY_ENABLED",
        "PIPELINE_PARALLEL_ENABLED",
    ):
        assert not hasattr(s, removed), f"{removed} should have been removed"


def test_v3_memory_max_tokens_default():
    """The char-approx token cap default is 3000 (bumped 2026-05-20)."""
    s = Settings(_env_file=None)
    assert s.AGENT_V3_MEMORY_MAX_TOKENS == 3000


def test_v3_memory_max_tokens_override():
    s = Settings(_env_file=None, AGENT_V3_MEMORY_MAX_TOKENS=200)
    assert s.AGENT_V3_MEMORY_MAX_TOKENS == 200


def test_model_defaults_are_nova_lite():
    """SPEC-AGENT-V2-CLEANUP-001 — defaults work with no env override."""
    s = Settings(_env_file=None)
    assert s.AGENT_LLM_MODEL == "nova-lite"
