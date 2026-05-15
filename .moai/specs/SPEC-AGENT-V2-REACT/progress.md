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

## Wave 7 — Post-review P0/P1 fixes (code + security review)
- P0-1 fixed: react_loop.py invalid-args history entry now includes `args_full` so the 3-consecutive-identical-call infinite-loop guard fires for repeated invalid-arg calls; added args_full strip to the infinite_loop_guard early-return path
- P1-2 fixed: refine_search.py removed `missing_image_url_in_ctx` early return — text-only turns now proceed via empty-image_url dense-path pipeline call (mirrors search_products.py); image_url-present behavior unchanged
- P1-3 fixed: agent.py turn_no replaced placeholder `10` with `state.turn_no or (state.turn_no or 0) + 1` (matches react_loop.py emit convention)
- P1-4 fixed: analyze_image.py `_ssrf_ok` adds UNCONDITIONAL hard-deny (non-http(s) schemes / 127.0.0.0-8 / ::1 / localhost / 10. / 172.16-31. / 192.168. / 169.254.) BEFORE allowlist; allowlist retained as narrowing layer (REQ-AGENT-SEC-URL-001)
- P1-5 fixed: tool_registry.py validate_args adds isinstance enforcement for top_k/n/min_price/max_price (int, bool rejected) + brand_likes/brand_dislikes/keyword_likes/keyword_dislikes/event_types/options (list); type mismatch returns same failure shape → react_loop records as invalid_args
- Tests: tests/test_agent_v2/test_security.py added (27 parametrized cases: SSRF hard-deny + positive control + validate_args type checks)
- ruff check app/ + ruff format: all green; pytest tests/test_agent_v2/: 53 passed (was 26; +27 new)
- Scope: only react_loop.py / refine_search.py / agent.py / analyze_image.py / tool_registry.py + new test file touched; skipped items (JSON-malform streak, all P2) left as-is per instruction
- T-010 complete: V1 regression migrated — 5n flag-aware / 4n property / 17n skipif / 6n deleted; full suite green both flags

## Wave 8 — Bedrock (nova-lite) compatibility fix (2026-05-15)
- Runtime verification: langchain-openai 0.3.34 `bind_tools()` does NOT inject `tool_choice` when arg omitted (source-confirmed + real `.ainvoke()` against `nova-lite` returns valid `tool_calls`, `finish_reason=tool_calls`, no HTTP 400). gpt-4o-mini blocked only by OpenAI account quota (429) — request shape valid, unrelated to tool_choice.
- llm_client.py fix: explicit `bind_tools(..., tool_choice=None)` (lock the no-inject contract against future langchain bumps) + `ChatOpenAI(extra_body={"drop_params": True})` (LiteLLM strips provider-unsupported params for Bedrock instead of 400). Both no-op for OpenAI → AGENT_LLM_MODEL stays swappable. Minimal diff, lazy-singleton + fail-closed preserved.
- react_loop.py review: no Bedrock-incompatible assumptions broken (tool_call id pass-through, finish_reason not inspected, content-vs-tool_calls via langchain-normalized `.tool_calls`, JSON-malform retry provider-agnostic). 1 latent non-live note: `tc.get("id", it)` fallback would mismatch a Bedrock tool-use id IF LiteLLM omits the id — Nova always returns one (verified); not fixed (minimal-diff).
- plan.md: OQ-1 false Bedrock premise corrected (`gpt-4o-mini`→`nova-lite` default; Bedrock IS configured infra), OQ-2 Bedrock `tool_choice` caveat + fix documented, §1 summary table rows + HISTORY line added.
- Tests: tests/test_agent_v2/test_llm_client.py added (2 mock-level cases: no forced tool_choice + drop_params + fail-closed). pytest tests/test_agent_v2/: 55 passed. ruff check + format: green.

- agent_v2 hotfix: assistant tool_use turn now appended before tool_result (Bedrock Nova multi-turn 400 fix — Expected toolResult blocks)
