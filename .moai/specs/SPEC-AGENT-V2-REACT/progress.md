# Progress: SPEC-AGENT-V2-REACT

- Plan phase complete: plan.md authored (15 tasks, all 10 OQs resolved)

## Wave 1 — Cross-SPEC + Foundation (complete)
- T-000 complete: ToolCallPayload TypedDict + 20th event_type "tool_call" added to event_payloads.py (conversation_log.py needs no change — generic emit)
- T-001 complete: WorkingState +3 fields (agent_iterations / tool_call_history[_LIST_ADD] / agent_status Literal)
- T-002 complete: app/agents/ package + tool_registry.py (7 args + 7 result TypedDicts + REGISTRY + ToolMetadata + validate_args)
- T-008 complete: 6 env vars in config.py + agent_v2_react_enabled/agent_llm_model_configured in /health/ready (fail-closed via AGENT_LLM_MODEL)

## Wave 2 — Tools (complete)
- T-003a complete: analyze_image.py — vision.extract wrapper + SSRF guard
- T-003b complete: search_products.py — run_pipeline wrapper
- T-003c complete: refine_search.py — critique re-search, no internal evaluator (OQ-7 α)
- T-003d complete: update_taste.py — TasteProfileStore wrapper, source enum validated
- T-003e complete: ask_user_clarification.py — inline keyboard card, clarify:{axis}:{value}
- T-003f complete: get_recent_history.py — conv_log SELECT + OQ-8 per-event whitelist, in_memory fail-soft
- T-003g complete: respond.py — adapter send_text + card pass-through, no _Flow enum

## Wave 3 — Core engine + graph integration (complete)
- T-004 complete: llm_client.py (bind_tools, fail-closed) + react_loop.py (iter cap, infinite-loop guard, token budget, per-LLM/tool timeout, JSON malform retry, args validation, tool_call emit, fallback respond)
- T-005 complete: nodes/agent.py — @observe span, react_loop wrapper, never propagates
- T-006 complete: fashion_bot.py V2 topology branch (flag+model gated), V1 byte-identical in else; _route_after_ingest_v2 / _route_after_pick_v2 / _route_after_vision_v2
- T-007 complete: ingest.py Step C — inline clarify:* boost_keywords (V2 flag), mid-onboarding → node_error

## Wave 4 — Deprecation markers + V2.1 stub (complete)
- T-014 complete: 5 modules (critique_apply/taste_update/respond/evaluator/router) DEPRECATED docstring + @MX:LEGACY; SPEC-AGENT-V2-CLEANUP-001 stub created (P3, draft)

## Wave 5 — Tests (partial — foundation coverage)
- T-009 partial: 4 of 9 test files created (test_state_extension, test_tool_registry, test_topology, test_agent_loop) — 26 tests green. Deferred: test_backward_compat, test_tool_call_logging, test_failure_modes, test_performance, test_security (see follow-ups)

## Wave 6 — Quality gate (complete for delivered scope)
- ruff check + ruff format: all green on new/changed files
- pytest tests/test_agent_v2/: 26 passed
- Full suite flag=false: 606 passed / 10 failed (all 10 pre-existing — verified via git stash on unmodified HEAD; 7 = test_critique_loop+routing env-broken, 3 = expected count-bump tests now fixed)
- Forbidden-path scope check: PASS (no onboard_/pipeline/providers/webhooks/recommend/clarify.py touched)
