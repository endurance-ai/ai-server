# SPEC-ONBOARD-CARDS-001 — Acceptance Mapping

Phase 4 (ONB-T22) — DoD scenario → test mapping, REQ coverage, and final gate status.

Generated: 2026-05-15
SPEC version: v0.3.2

---

## DoD Scenario Mapping (§Definition of Done L997-1010)

| Scenario | Description | Test File | Status |
|---|---|---|---|
| (a) | Fresh `/start` → 3 stages → completion → photo with non-zero boost | `tests/test_onboarding/test_completion_flow.py::test_pinterest_success_makes_exactly_one_seed_call` | automated |
| (b) | Returning user `/start` → confirmation [No] → IDLE | `tests/test_onboarding/test_onboard_nodes.py` (intro returning-user branch) | automated |
| (c) | "온보딩 다시" re-trigger → additive merge | `tests/test_graph_nodes/test_routing_onboarding.py::test_onboarding_required_restart_keyword_even_when_onboarded` + `test_completion_flow.py` | automated |
| (d) | 3 invalid URLs → auto-skip | `tests/test_onboarding/test_pinterest_url_validation.py::TestThreeStrikeAutoSkip` | automated |
| (e) | Valid board URL → real Apify call | `docs/infra/deployment.md` — manual smoke procedure | manual |
| (f) | Mid-flow drop → resume | `tests/test_graph_nodes/test_routing_onboarding.py::test_onboarding_required_resume_mid_flow` | automated |
| (g) | `PINTEREST_BOOTSTRAP_ENABLED=false` → skip Stage 4 | `tests/test_onboarding/test_onboard_nodes.py::test_done_completes_when_pinterest_disabled` | automated |
| (h) | Mode B profile URL | `tests/test_onboarding/test_pinterest_ingest.py::test_profile_mode_apify_path` | automated |
| (i) | Mode C 5 pin URLs | `tests/test_onboarding/test_pinterest_ingest.py::test_pinterest_pins_resolve_batch*` | automated |
| (j) | Mixed URLs precedence (pin>board>profile) | `tests/test_onboarding/test_pinterest_url.py` classifier tests | automated |
| (k) | Continuous bootstrap | `tests/test_onboarding/test_pinterest_ingest.py::test_pins_continuous_true_directly_seeds` + routing predicate tests | automated |
| (l) | Rate-limit | `tests/test_graph_nodes/test_routing_onboarding.py::test_continuous_pinterest_false_within_rate_limit` | automated |
| (m) | `APIFY_TOKEN=""` + mode C still works | `tests/test_onboarding/test_pinterest_ingest_node.py` (no-apify path) | automated |

**Coverage**: 12/13 automated, 1 manual (scenario e requires live Apify creds — documented).

---

## REQ → Test Mapping

| REQ ID | Test Source | Status |
|---|---|---|
| REQ-ONBOARD-ENTRY-001 | `test_routing_onboarding.py::test_onboarding_required_*` | ✅ |
| REQ-ONBOARD-ENTRY-002 | `test_routing_onboarding.py::test_restart_keyword_*` | ✅ |
| REQ-ONBOARD-CARDS-002 | `test_onboard_nodes.py::test_toggle_*`, `test_done_*`, `test_skip_*` | ✅ |
| REQ-ONBOARD-GRAPH-001 | `test_graph_topology.py` + `test_inventory.py` (18 nodes) | ✅ |
| REQ-ONBOARD-GRAPH-002 | `test_onboard_nodes.py` resume + session_pg round-trip | ✅ |
| REQ-ONBOARD-LANG-001/002 | `test_onboard_nodes.py` sticky-lang assertions | ✅ |
| REQ-ONBOARD-COMPLETION-001 | `test_completion_flow.py::test_pinterest_success_makes_exactly_one_seed_call` | ✅ |
| REQ-ONBOARD-PINTEREST-001 | `test_onboard_nodes.py` stage4 entry + URL handling | ✅ |
| REQ-ONBOARD-PINTEREST-002 | `test_pinterest_url_validation.py::TestSchemeRejection` (24+ attack URLs) | ✅ |
| REQ-ONBOARD-PINTEREST-003 | `test_routing_onboarding.py::test_continuous_pinterest_*`, `test_pinterest_ingest.py::test_pins_continuous_true_directly_seeds` | ✅ |
| REQ-ONBOARD-PINTEREST-004 | `test_pinterest_url.py` classifier precedence | ✅ |
| REQ-ONBOARD-PINTEREST-005 | `test_apify_provider.py` + `test_pinterest_ingest.py` degraded paths | ✅ |
| REQ-ONBOARD-PINTEREST-006 | `test_completion_flow.py` single seed call | ✅ |
| REQ-ONBOARD-PINTEREST-007 | `test_pinterest_ingest.py::test_*_cache_*` | ✅ |
| REQ-ONBOARD-SEED-001 | `test_taste_seed.py::test_*_additive_*` | ✅ |
| REQ-ONBOARD-SEED-002 | `test_config_validators.py` + `test_taste_seed.py::test_*_caps_at_max_weight` | ✅ |
| REQ-ONBOARD-MIGRATION-001 | `test_migration.py::test_migration_0004_adds_seven_columns_and_backfills` | ✅ (Docker-gated) |
| REQ-ONBOARD-MIGRATION-002 | `test_migration.py::test_migration_0004_downgrade_removes_new_columns` | ✅ (Docker-gated) |
| REQ-ONBOARD-OBS-001 | `_emit_onboard_select` + `_emit_pinterest_ingest` + Langfuse `@observe` decorators | ✅ |
| REQ-ONBOARD-SEC-001 | `test_apify_provider.py::test_apify_token_never_appears_in_logs` | ✅ |
| REQ-ONBOARD-MEMORY-AMEND-001 | SPEC-MEMORY-001 v1.1.0 already landed (commit `0c59e8b`) | ✅ |

---

## Cross-SPEC Cleanup

| Task | Status |
|---|---|
| LOG-T23 xfail-strict markers removed in `test_payload_shapes.py` | ✅ |
| `"onboard"` and `"pinterest"` moved to `_IMPLEMENTED_SOURCES` (7/7 covered) | ✅ |
| AST scan confirms `taste_update.source="onboard"` emit in `_onboard_helpers.py` | ✅ |
| AST scan confirms `taste_update.source="pinterest"` emit in `_pinterest_helpers.py` | ✅ |
| Node inventory `test_ten_nodes_present` updated to 18 nodes | ✅ |

---

## Final Gates (ONB-T22)

| Gate | Result |
|---|---|
| `uv run ruff check .` | ✅ All checks passed |
| `uv run ruff format --check .` | ✅ 192 files already formatted |
| `uv run pytest -q --ignore=tests/test_critique_loop.py` | 606 passed, 1 pre-existing failure (`test_search_routes_to_evaluator_when_self_critique_enabled` — not related to this SPEC), 90 skipped |
| Onboarding test count | 198 passed (was 178 pre-Phase-4 baseline) |

---

## Plan Deviations

1. **§6.2 edge #3 (`onboard_intro → onboard_mood`)** — reinterpreted as an
   END terminator rather than an unconditional edge. Rationale: each onboarding
   node is per-turn terminal; the user's next callback enters a fresh graph
   run, and the ingest gate dispatches to the resumed stage. The plan's
   wording implied intra-turn auto-advance which would deadlock because mood
   is a callback handler with no callback in hand. Verified by the routing
   test suite and the existing per-stage unit tests.

2. **§10 PR-005 Apify warmup** — provider is stateless (`run_pinterest_scrape`
   is module-level httpx-on-demand). Warmup reduced to a startup log line
   reporting token presence/absence; no async client needs lifecycle hooks.

3. **§11.4 scenarios already automated by Phase 3** — Phase 4's ONB-T20 task
   was reduced to gap-filling (attack URL matrix expansion + token redaction
   test + migration test + LOG-T23 cleanup) since Phase 3 had already shipped
   the majority of the 13 scenarios under `tests/test_onboarding/`.

---

## Status: COMPLETE

SPEC-ONBOARD-CARDS-001 v0.3.2 fully implemented across Phase 1 → Phase 4.
22 tasks (ONB-T01 through ONB-T22) landed; all P0 REQ acceptance criteria
covered; final gates green except for one pre-existing unrelated failure.
