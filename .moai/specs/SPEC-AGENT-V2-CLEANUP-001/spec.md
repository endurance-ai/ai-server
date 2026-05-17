# SPEC-AGENT-V2-CLEANUP-001 — V2.1 cleanup of deprecated agent V1 modules

- **Status**: draft (stub)
- **Priority**: P3
- **Predecessor**: SPEC-AGENT-V2-REACT v0.1.1 (creates deprecation markers)
- **Trigger**: SPEC-AGENT-V2-REACT V2.0 prod cutover stable for ≥ 30 days

## Scope

Remove the following modules and helpers from the codebase. All are marked
DEPRECATED in SPEC-AGENT-V2-REACT V2.0:

### Modules to delete

1. `app/graphs/nodes/critique_apply.py` — body lives in `app/agents/tools/refine_search.py`
2. `app/graphs/nodes/taste_update.py` — body lives in `app/agents/tools/update_taste.py`
3. `app/graphs/nodes/respond.py` — body lives in `app/agents/tools/respond.py`
4. `app/graphs/nodes/evaluator.py` — folded into `refine_search` (OQ-7 α)
5. `app/channels/router.py` — agent LLM reasoning subsumes deterministic text routing

### Helpers to delete from `app/graphs/fashion_bot.py`

- `_router_text_passthrough`
- `_apply_self_critique_passthrough`
- V1 build_graph body (only V2 topology remains)

### Routing functions to delete from `app/graphs/routing.py`

- `_route_after_router_text`
- `_route_after_critique`
- `_route_after_evaluator`
- `_route_after_search` (V1 variants)

### Env vars to deprecate

- `AGENT_V2_REACT_ENABLED` — V2 becomes unconditional
- `SELF_CRITIQUE_*` family — evaluator removed

## Out of scope

- Onboarding subgraph (unchanged from SPEC-ONBOARD-CARDS-001 v0.3.2)
- Vision pre-agent step (OQ-3 decision retained)
- Test files for deprecated modules — migrated in SPEC-AGENT-V2-REACT T-010

## Risks

- R1: Re-introducing V1 path for emergency rollback becomes harder once
  this SPEC merges. Mitigation: 30-day post-cutover stability gate.
- R2: External integrations referencing removed envs. Mitigation: deprecation
  notice in `/health/ready` JSON for 1 release before removal.

## Cross-SPEC

- SPEC-AGENT-V2-REACT — V2.0 implementation (predecessor)
- SPEC-AGENTIC-CRITIQUE-001 — affected by OQ-7 α (partial supersede)
- SPEC-AGENT-001 — original 10-node topology origin

## Acceptance

- 5 modules deleted, no import errors
- `pytest -q` green
- `grep -r "AGENT_V2_REACT_ENABLED"` = 0 references in code
- CI passes under any value of removed env vars
