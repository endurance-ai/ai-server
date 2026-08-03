"""SPEC-AGENT-V3-REACT / SPEC-AGENT-V2-CLEANUP-001 — shared test isolation.

The V3 enhancements (memory injection, Reflexion, proactive suggest,
cross-thread dislike) are now UNCONDITIONAL — the AGENT_V2_REACT_ENABLED /
AGENT_V3_*_ENABLED feature flags were removed. This fixture no longer
snapshots/restores those flags or rebuilds a flag-aware tool REGISTRY; it only

  - snapshots the remaining tunable settings attrs a V3 test may mutate and
    restores them verbatim (no cross-test bleed),
  - resets the `taste_profile` store singleton before and after every test.
"""

from __future__ import annotations

import pytest

# Remaining (non-flag) settings attrs a V3 test may mutate — snapshot+restore.
_GUARDED_SETTINGS_ATTRS = (
    "AGENT_V3_MEMORY_MAX_TOKENS",
    "AGENT_MAX_ITERATIONS",
    "AGENT_LLM_TIMEOUT_S",
    "AGENT_TOOL_TIMEOUT_S",
    "AGENT_TURN_TOKEN_BUDGET",
    "AGENT_RESPOND_TIMEOUT_S",
    "SELF_CRITIQUE_MAX_ITERATIONS",
    "EVALUATOR_TIMEOUT_S",
)


@pytest.fixture(autouse=True)
def _v3_isolation():
    """Function-scoped: snapshot/restore the guarded tunable attrs and reset
    the taste-store singleton. Runs for EVERY test under tests/test_agent_v3/.
    """
    from app.core import config as _cfg

    try:
        _cfg.get_settings.cache_clear()
    except AttributeError:
        pass
    live = _cfg.settings

    snapshot = {a: getattr(live, a) for a in _GUARDED_SETTINGS_ATTRS if hasattr(live, a)}

    from app.infrastructure.memory import taste_profile as _tp

    _tp._store = None

    # 260611 — process-global `last_query._LAST` leaks across tests when
    # other suites (e.g. test_agent_v2) drive a `search_products` dispatch
    # that calls `set_last_query`. `_build_memory_context` now surfaces
    # `last_search_query: …`, so a leaked value contaminates the
    # `(no taste history yet)` placeholder assertion in
    # `test_ac_1_2_fail_soft_empty`. Reset before AND after.
    try:
        from app.agents import last_query as _lq

        _lq._reset_all_for_tests()
    except Exception:  # noqa: BLE001 — defensive (import path drift)
        pass

    yield

    for a, v in snapshot.items():
        try:
            setattr(live, a, v)
        except Exception:  # noqa: BLE001 — pydantic v2 guard fallback
            object.__setattr__(live, a, v)
    _tp._store = None
    try:
        from app.agents import last_query as _lq

        _lq._reset_all_for_tests()
    except Exception:  # noqa: BLE001
        pass
