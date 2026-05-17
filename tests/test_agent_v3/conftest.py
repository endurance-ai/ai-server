"""SPEC-AGENT-V3-REACT — shared test isolation fixtures.

BLOCKING-1 root cause (Phase 2.8a evaluator): every V3 test mutates the
module-level lru_cache'd `settings` singleton via `monkeypatch.setattr`.
Under the FULL `uv run pytest` ordering this leaks two ways:

  1. A V3 test that flips e.g. `AGENT_V3_PROACTIVE_ENABLED=True` (and rebuilds
     the module-load tool REGISTRY) can leak that state into unrelated suites.
  2. If another suite has imported / reloaded a *stale* `app.core.config`
     before the V3 fields existed in that interpreter view, Pydantic v2's
     `__setattr__` rejects the unknown field ("Settings object has no field
     AGENT_V3_..."), turning every V3 fixture into an ERROR.

Fix: one autouse, function-scoped fixture for the whole `tests/test_agent_v3/`
package that

  - clears the `get_settings` lru_cache and rebinds every module that did
    `from app.core.config import settings` to ONE live instance that is
    guaranteed to carry the V3 fields (defensive re-sync),
  - snapshots every settings attribute a V3 test may touch and restores it
    verbatim afterwards (no cross-test / cross-suite bleed),
  - resets the `taste_profile` store singleton and the flag-aware tool
    REGISTRY back to the V2 7-tool baseline after every test.

All V3 test files now rely on this — the per-file `_force_all_off` /
`_reset_taste_store` / `_proactive_on` helpers were removed (DRY + single
source of isolation truth).
"""

from __future__ import annotations

import pytest

# Settings attributes any V3 test mutates — snapshot+restore around each test.
_GUARDED_SETTINGS_ATTRS = (
    "AGENT_V3_MEMORY_INJECTION_ENABLED",
    "AGENT_V3_REFLEXION_ENABLED",
    "AGENT_V3_PROACTIVE_ENABLED",
    "AGENT_V3_DISLIKE_MEMORY_ENABLED",
    "AGENT_V3_MEMORY_MAX_TOKENS",
    "AGENT_V2_REACT_ENABLED",
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
    """Function-scoped: guarantee a V3-aware live `settings`, snapshot/restore
    all guarded attrs, and reset the taste-store + tool REGISTRY singletons.

    This runs for EVERY test under tests/test_agent_v3/ regardless of file.
    """
    from app.core import config as _cfg

    # 1. Defensive re-sync: ensure the live singleton actually carries the V3
    #    fields. If a stale class view leaked in (no V3 fields), rebuild from a
    #    fresh Settings() and rebind every importer's reference.
    try:
        _cfg.get_settings.cache_clear()
    except AttributeError:
        pass
    live = _cfg.settings
    if not all(hasattr(live, a) for a in ("AGENT_V3_MEMORY_INJECTION_ENABLED",)):
        live = _cfg.Settings()
        _cfg.settings = live
        # Rebind modules that did `from app.core.config import settings`.
        import sys

        for modname in (
            "app.agents.react_loop",
            "app.agents.tool_registry",
            "app.agents.tools.update_taste",
            "app.agents.tools.search_products",
            "app.channels.taste_profile",
        ):
            mod = sys.modules.get(modname)
            if mod is not None and hasattr(mod, "settings"):
                mod.settings = live

    # 2. Snapshot guarded settings attrs.
    snapshot = {a: getattr(live, a) for a in _GUARDED_SETTINGS_ATTRS if hasattr(live, a)}

    # 3. Reset taste-store singleton + tool REGISTRY to V2 baseline (pre-test).
    from app.channels import taste_profile as _tp

    _tp._store = None
    try:
        from app.agents import tool_registry as _tr

        _tr._rebuild_registry_for_flag(False)
    except Exception:  # noqa: BLE001 — registry helper always present in tree
        pass

    yield

    # 4. Restore — guarantees zero cross-test / cross-suite bleed.
    for a, v in snapshot.items():
        try:
            setattr(live, a, v)
        except Exception:  # noqa: BLE001 — pydantic v2 guard fallback
            object.__setattr__(live, a, v)
    _tp._store = None
    try:
        from app.agents import tool_registry as _tr

        _tr._rebuild_registry_for_flag(False)
    except Exception:  # noqa: BLE001
        pass
