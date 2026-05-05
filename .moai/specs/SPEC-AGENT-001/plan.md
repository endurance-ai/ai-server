---
id: SPEC-AGENT-001
artifact: plan
version: 0.1.0
status: draft
created: 2026-05-05
updated: 2026-05-05
author: hchsa77@gmail.com
methodology: TDD (RED-GREEN-REFACTOR per task; user override of project default `development_mode: ddd`)
---

# Implementation Plan — SPEC-AGENT-001 LangGraph Agent Migration

This plan resolves the six explicit Open Questions in `spec.md`, decomposes the
migration into atomic TDD tasks, and pins down the test-mapping + risk coverage
matrices the SPEC requires the PR description to carry. No code is written
here; this document is the input contract for `manager-tdd`.

The SPEC fixes WHAT the graph must do (REQ-AGENT-*, REQ-STATE-*, REQ-LLM-*,
REQ-OBSV-*, REQ-MIGR-*, REQ-COMPAT-*). This plan fixes HOW we will reach that
state, in what order, with which library version pins.

---

## Section 1 — Open Question Resolutions

The SPEC's "Open Questions" block leaves six decisions to `plan.md`. Each is
resolved below with a concrete pin, a 1-3 sentence rationale, and the REQ it
satisfies.

### Q1. Langfuse handler import path

**Decision**: `from langfuse.callback import CallbackHandler`.

**Rationale**: The repo currently pins `langfuse>=2.50,<3.0` in
`pyproject.toml` and `uv.lock` resolves to `langfuse 2.60.10`. The self-hosted
Langfuse server runs on the v2 image (per the existing comment in
`pyproject.toml`), so we MUST stay on the v2 SDK (v3 ingestion endpoints break
against a v2 server). On Langfuse 2.x the LangChain handler ships **inside**
the main `langfuse` package at `langfuse.callback.CallbackHandler` — there is
no `langfuse[langchain]` extra and no separate `langfuse-langchain`
distribution on the v2 line. (Those paths exist only on v3+.)

**Pyproject change**: NONE for langfuse-langchain. The `langfuse>=2.50,<3.0`
line already covers the handler. We only need the three new direct deps
(`langgraph`, `langchain-core`, `langchain-openai`).

**Compat with `app/observability/langfuse.py`**: The existing file already
falls back across v2/v3 `observe()` paths. We extend it minimally to also
expose a `build_callback_handler(trace_id, session_id, user_id) -> Optional[Handler]`
factory that returns `None` when keys are absent (no-op fallback preserved).

**Satisfies**: REQ-AGENT-003, REQ-OBSV-002, R11.

---

### Q2. ChatOpenAI instance sharing — module singleton vs per-call

**Decision**: **Two independent module-level singletons**, one per node file
(`respond.py`, `ask_clarify.py`), constructed at import time. Different
temperatures and `max_tokens` per node.

**Rationale**: Each `ChatOpenAI` instance carries its own `temperature`,
`max_tokens`, and connection pool; instantiating per-call wastes the httpx
pool and adds ~10ms of object construction per webhook. Independent instances
let `respond` (warmer, longer) and `ask_clarify` (cooler, shorter) tune
independently. Sharing a single instance and overriding via `bind()` is
possible but adds an indirection that the SPEC forbids ("two `ChatOpenAI`
instances exist" — REQ-LLM-002 acceptance criterion 1).

**Pinned hyperparameters** (documented here per REQ-LLM-002 acceptance #3):

| Node | model | temperature | max_tokens | timeout |
|------|-------|-------------|------------|---------|
| `respond` | `settings.RESPONSE_MODEL` (default `gpt-4o-mini`) | `0.7` | `settings.RESPONSE_MAX_TOKENS` (default `200`) | `settings.RESPONSE_TIMEOUT_MS / 1000` |
| `ask_clarify` | `settings.RESPONSE_MODEL` | `0.4` | `80` (smaller cap, hard-coded; documented per REQ-LLM-003 footnote) | `settings.RESPONSE_TIMEOUT_MS / 1000` |

`base_url = settings.LITELLM_BASE_URL + "/v1"`,
`api_key = settings.LITELLM_MASTER_KEY`. No direct OpenAI credentials.

**Test isolation**: Both module-level singletons are easily monkeypatched in
unit tests via `monkeypatch.setattr("app.graphs.nodes.respond._llm", fake_llm)`
following the existing pattern used in `tests/test_router.py`.

**Satisfies**: REQ-LLM-002, REQ-LLM-003, REQ-LLM-004, REQ-LLM-005.

---

### Q3. Graph compile timing — module-level vs lazy first-call

**Decision**: **Module-level compile**, cached as `GRAPH = _build_graph().compile()`
in `app/graphs/fashion_bot.py` at import time. The webhook handler imports
`GRAPH` and calls `await GRAPH.ainvoke(state, config=run_config)`.

**Rationale**: REQ-AGENT-001 acceptance criterion 4 explicitly mandates "compile
cached at module level". Lazy compilation introduces a first-webhook latency
spike (graph compile ~50-150 ms on LangGraph 1.x) and complicates Langfuse
trace timing on the cold path. The compile is pure (no I/O), so there is no
startup-cost objection; it adds one-time cost at FastAPI lifespan startup
(amortized across the process lifetime).

**Test-isolation pattern** (mitigates R7 — "compiled graph cached at module
import could leak state across tests"):

- Tests that need a custom topology or that need to re-compile after
  monkeypatching a node SHALL use a **builder factory**, not `importlib.reload`.
  We expose `build_graph()` as a public function alongside the module-level
  `GRAPH` singleton:

  ```
  # app/graphs/fashion_bot.py (sketch — illustrative only, no code change here)
  def build_graph() -> CompiledGraph: ...
  GRAPH = build_graph()
  ```

  Tests instantiate a fresh graph via `build_graph()` when they need to swap
  in a fake node; production code always uses `GRAPH`.

- Avoid `importlib.reload(app.graphs.fashion_bot)` because it interacts poorly
  with langfuse's module-level callback handlers and would also reload the
  two `ChatOpenAI` singletons (Q2).

**Satisfies**: REQ-AGENT-001, R7 mitigation.

---

### Q4. `messages` reducer producers — which upstream nodes append

**Decision**: Three nodes append, capped at three messages per webhook to keep
the `respond` prompt bounded.

| Producer node | Message kind | Content (one-line summary) |
|---|---|---|
| `vision_node` (success path) | `SystemMessage` | `"vision: detected {n} item(s); primary={label}"` |
| `critique_apply` (text or callback path) | `SystemMessage` | `"critique: {compact_delta_repr}"` (e.g. `"price_max=80, exclude=[brand_x]"`) |
| `search_node` (after pipeline) | `SystemMessage` | `"search: {len(candidates)} candidate(s) after diversify+post-filter"` |

`respond` and `ask_clarify` do NOT append — they consume `messages` and emit
`AIMessage` only as the LLM output (which langchain's `Runnable` handles
internally; we do NOT manually push it back into state).

**Why these three and only these three**:

1. The three are the only nodes that produce information `respond` cannot
   trivially recompute from `WorkingState.detected_items`,
   `WorkingState.critique_delta`, and `WorkingState.candidates`. Adding more
   producers (e.g. `ingest`, `taste_update`) bloats the prompt without
   improving copy quality.
2. Worst-case message count on any flow is exactly 3
   (vision→critique→search). The picker-only and ask_clarify flows skip
   `respond` entirely (REQ-AGENT-006), so reducer growth is structurally
   bounded — no truncation logic needed in `respond`.
3. Cap is documented here per R10 mitigation; if a future flow exceeds 3
   messages, `plan.md` is the place to revisit (not the node bodies).

**Off-topic / link-fail / vision-fail flows** route directly to `respond` from
`ingest` / `resolve_image` / `vision_node` fallback branch — they carry zero
messages, and `respond` falls back to its hard-coded English templates per
REQ-LLM-004.

**Satisfies**: REQ-STATE-002 (`messages: Annotated[..., add_messages]`),
R10 mitigation.

---

### Q5. Topology assertion format for REQ-AGENT-005

**Decision**: **Structural edge-set assertion**, NOT a Mermaid string snapshot.

**Rationale**: A Mermaid snapshot is brittle against whitespace, comment
reordering, and node-name capitalization changes that have zero semantic
impact. LangGraph 1.x exposes `compiled_graph.get_graph()` which returns a
`Graph` object with `.nodes` (dict) and `.edges` (list of `Edge` namedtuples
with `.source`, `.target`, `.data`, `.conditional`). Asserting against this
introspection gives the same coverage with zero false positives on
formatting drift.

**Sketch of the test** (in `tests/test_graph_topology.py`):

```
# Illustrative — exact API names checked against langgraph 1.1.10 at task T-019.
from app.graphs.fashion_bot import build_graph

def test_topology_edges_match_spec():
    g = build_graph().get_graph()
    node_names = set(g.nodes.keys())
    assert node_names == {
        "ingest", "resolve_image", "vision_node", "pick_item",
        "critique_apply", "search_node", "send_results",
        "taste_update", "ask_clarify", "respond",
        "__start__", "__end__",
    }

    # Unconditional edges
    unconditional = {(e.source, e.target) for e in g.edges if not e.conditional}
    assert ("__start__", "ingest") in unconditional
    assert ("critique_apply", "search_node") in unconditional
    assert ("send_results", "respond") in unconditional
    assert ("taste_update", "respond") in unconditional
    assert ("respond", "__end__") in unconditional
    assert ("ask_clarify", "__end__") in unconditional

    # Conditional edge sources (presence only — branch logic covered by
    # routing.py unit tests in test_graph_nodes/test_routing.py)
    cond_sources = {e.source for e in g.edges if e.conditional}
    assert cond_sources == {
        "ingest", "resolve_image", "vision_node",
        "pick_item", "search_node",
    }
```

If LangGraph 1.1.10's introspection turns out to expose a slightly different
shape (e.g. `g.edges` returns `(source, target, data)` tuples), the test is
trivial to adapt — the assertion still operates on Python objects, not on a
rendered string.

**Mermaid as documentation, not as test**: We continue to render the Mermaid
diagram in `spec.md` for human readers; the test does NOT diff against it.

**Satisfies**: REQ-AGENT-005 acceptance criterion 9.

---

### Q6. `presearch_summary` producer

**Decision**: `critique_apply` writes `presearch_summary` (a one-line summary
of the applied delta, e.g. `"applied: price_max=80, exclude=[brand_x]"`).
For flows that bypass `critique_apply` (off-topic, link-fail, vision-fail,
taste-only), `presearch_summary` stays `None`.

**Rationale**: The SPEC describes `presearch_summary` as "consumed by `respond`
for context" (REQ-STATE-002 row 9). `critique_apply` is the only node that
produces information `respond` would otherwise have to re-derive from
`WorkingState.critique_delta` (which is structured, not human-readable). For
non-critique flows, `respond` already has all the context it needs:
`detected_items` for vision-success, hard-coded templates for vision-fail /
link-fail, and the router decision for off-topic.

**Why not `vision_node`**: vision output is already in `WorkingState.detected_items`
(structured) and `WorkingState.messages` (the `SystemMessage` summary from
Q4). Duplicating it into `presearch_summary` adds noise.

**Why not "leave it None for v0.1.0"**: This is a one-line implementation in
`critique_apply` (build the summary inline alongside `WorkingState.critique_delta`),
and the Langfuse trace metadata `delta` field (REQ-OBSV-003) needs the same
string anyway. Building it once and reading from `WorkingState.presearch_summary`
in both the `respond` prompt and the trace metadata builder is strictly less
work than building it in two places.

**Satisfies**: REQ-STATE-002 row 9, REQ-OBSV-003 row 3 (`delta` metadata
shares the same string).

---

## Section 2 — Task Decomposition

Twenty atomic tasks, each completable in one TDD RED-GREEN-REFACTOR cycle. The
sequence is dependency-driven: foundation (deps + state) → routing →
node-by-node implementation → graph wiring → caller swap → cleanup → verification.

`development_mode: ddd` is set in `quality.yaml`; the user has overridden to
TDD for this SPEC because the migration target (the new graph) is greenfield
code and the existing modules are already covered. Each task below states the
RED test name first, then the GREEN scope, then the REFACTOR opportunity.

| Task ID | Description | REQ Mapping | Dependencies | Planned Files | Status |
|---|---|---|---|---|---|
| T-001 | Add 4 runtime deps + 5 env vars. RED: import test asserting `langgraph`, `langchain_core`, `langchain_openai` import successfully and that `settings.RESPONSE_MODEL`, `RESPONSE_TIMEOUT_MS`, `RESPONSE_MAX_TOKENS`, `ASK_CLARIFY_MIN_DESC_TOKENS`, `ASK_CLARIFY_AMBIGUOUS_LABELS` are exposed with documented defaults. GREEN: edit `pyproject.toml` + run `uv lock`; edit `app/core/config.py` + `.env.example`. No langfuse-langchain extra (Q1: handler is in core `langfuse` 2.x at `langfuse.callback`). REFACTOR: alphabetize Settings fields. | REQ-AGENT-001, REQ-AGENT-002, REQ-AGENT-003, REQ-LLM-003 | none | `pyproject.toml`, `uv.lock`, `app/core/config.py`, `.env.example`, `tests/test_config.py` | pending |
| T-002 | `app/graphs/state.py` — `InputState`, `WorkingState`, `OutputState`. RED: `tests/test_graph_state.py` asserts (a) `InputState` rejects extra fields, (b) `WorkingState` defaults are exactly per REQ-STATE-002 table, (c) two deltas with `log_events` concatenate via the reducer, (d) `messages` uses `add_messages`, (e) `OutputState.sent_count >= 1` invariant validator. GREEN: write the three Pydantic v2 models. REFACTOR: extract the reducer imports (`operator.add`, `langgraph.graph.message.add_messages`) into a private module-level alias for readability. | REQ-STATE-001, REQ-STATE-002, REQ-STATE-003, REQ-STATE-004 | T-001 | `app/graphs/__init__.py`, `app/graphs/state.py`, `tests/test_graph_state.py` | pending |
| T-003 | `app/graphs/routing.py` — six routing functions (`_route_after_ingest`, `_route_after_router_text`, `_route_after_resolve`, `_route_after_vision`, `_route_after_pick`, `_route_after_search`). RED: `tests/test_graph_nodes/test_routing.py` parametrized over the full Mermaid topology — one assertion per branch listed in REQ-AGENT-005 (estimated 18 branch assertions). GREEN: implement each function as a pure `def(state) -> str` returning a node name. REFACTOR: collapse repeated `RoutedIntent` matching into a small dispatch table. | REQ-AGENT-005, REQ-AGENT-009 (vision branch), REQ-AGENT-010 (picker branch) | T-002 | `app/graphs/routing.py`, `tests/test_graph_nodes/test_routing.py` | pending |
| T-004 | Node 1/10 — `app/graphs/nodes/ingest.py`. RED: `tests/test_graph_nodes/test_ingest.py` covers (a) photo → sets `image_url`, (b) url-only → sets `image_url`, (c) callback `crit:*` parsed, (d) callback `item:N` parsed, (e) text in `RESULTS_SENT` invokes `router.route_text` and writes `decision`, (f) text in `AWAITING_INTENT` skips router, (g) error path: `router.route_text` raises → empty delta + log event (REQ-AGENT-007). GREEN: thin wrapper around existing classify logic from `scenario.handle`. REFACTOR: extract the 5-way branch into a small `_classify_inbound(state)` helper. | REQ-AGENT-004 (row 1), REQ-AGENT-007, REQ-COMPAT-002 | T-003 | `app/graphs/nodes/__init__.py`, `app/graphs/nodes/ingest.py`, `tests/test_graph_nodes/test_ingest.py` | pending |
| T-005 | Node 2/10 — `app/graphs/nodes/resolve_image.py`. RED: `tests/test_graph_nodes/test_resolve_image.py` covers (a) Pinterest URL resolves to og:image, (b) raw image URL passthrough, (c) `link_resolver.resolve` raises → `image_url=None` + log event (REQ-AGENT-007), (d) timeout. GREEN: import `link_resolver.resolve` and call. REFACTOR: none anticipated (single-line wrapper). | REQ-AGENT-004 (row 2), REQ-AGENT-007, REQ-MIGR-005 | T-002 | `app/graphs/nodes/resolve_image.py`, `tests/test_graph_nodes/test_resolve_image.py` | pending |
| T-006 | Node 3/10 — `app/graphs/nodes/vision.py`. RED: `tests/test_graph_nodes/test_vision_node.py` covers (a) clear single-item → `detected_items[0]` populated + `SystemMessage` appended to `messages` (Q4), (b) multi-item → `detected_items` length > 1, (c) ambiguous label per `ASK_CLARIFY_AMBIGUOUS_LABELS` env, (d) short description per `ASK_CLARIFY_MIN_DESC_TOKENS` env, (e) `vision.extract` raises `httpx.TimeoutException` → empty delta + log (REQ-AGENT-007). GREEN: import `vision.extract`; the routing decision is in T-003 not here. REFACTOR: factor the `SystemMessage` summary builder into a private helper. | REQ-AGENT-004 (row 3), REQ-AGENT-007, REQ-AGENT-009 (the predicate fields written here; routing read in T-003), REQ-MIGR-005 | T-002 | `app/graphs/nodes/vision.py`, `tests/test_graph_nodes/test_vision_node.py` | pending |
| T-007 | Node 4/10 — `app/graphs/nodes/pick_item.py`. RED: `tests/test_graph_nodes/test_pick_item.py` covers (a) picker carousel sent via fake adapter when `selected_item_index is None`, (b) when callback `item:N` provided, sets `WorkingState.detected_items[N]` as the chosen item and lets the routing function (T-003) emit `critique_apply`, (c) adapter raises → empty delta + log (REQ-AGENT-007). GREEN: lift `handle_visual_intake_picker` and `handle_pick_choice` from `scenario.py` into a single thin node. REFACTOR: extract `_render_picker_card_list(items)` into a private module-level helper. | REQ-AGENT-004 (row 4), REQ-AGENT-007, REQ-AGENT-010, REQ-COMPAT-004 | T-002 | `app/graphs/nodes/pick_item.py`, `tests/test_graph_nodes/test_pick_item.py` | pending |
| T-008 | Node 5/10 — `app/graphs/nodes/ask_clarify.py`. RED: `tests/test_graph_nodes/test_ask_clarify.py` covers (a) on weak vision returns one short English question via stub `ChatOpenAI`, (b) adapter `send_text` called exactly once, (c) graph terminates without invoking `respond` (verified at integration in T-017), (d) LLM raises → fallback hard-coded English question dispatched (REQ-LLM-005). GREEN: instantiate module-level `ChatOpenAI` per Q2 (`temperature=0.4`, `max_tokens=80`). REFACTOR: extract the prompt template into a `_PROMPT` module constant. | REQ-AGENT-004 (row 5), REQ-AGENT-009, REQ-LLM-002, REQ-LLM-005 | T-002, T-006 | `app/graphs/nodes/ask_clarify.py`, `tests/test_graph_nodes/test_ask_clarify.py` | pending |
| T-009 | Node 6/10 — `app/graphs/nodes/critique_apply.py`. RED: `tests/test_graph_nodes/test_critique_apply.py` covers (a) `crit:cheaper` callback → `CritiqueDelta(price_max=anchor*0.7)`, (b) `crit:less` callback with brand → `CritiqueDelta(exclude_brand=[brand_x])`, (c) text path with `RoutedIntent.critique_text` → delta from router output, (d) `presearch_summary` populated per Q6, (e) `SystemMessage` appended per Q4, (f) `parse_callback` raises → empty delta + log (REQ-AGENT-007). GREEN: import `critique.parse_callback` + `critique.merge_delta`. REFACTOR: extract the `presearch_summary` formatter into a `_format_delta_summary(delta)` helper (reused by REQ-OBSV-003 metadata builder in T-014). | REQ-AGENT-004 (row 6), REQ-AGENT-007, REQ-COMPAT-001, REQ-COMPAT-002, REQ-MIGR-005 | T-002 | `app/graphs/nodes/critique_apply.py`, `tests/test_graph_nodes/test_critique_apply.py` | pending |
| T-010 | Node 7/10 — `app/graphs/nodes/search.py`. RED: `tests/test_graph_nodes/test_search_node.py` covers (a) happy path: `pipeline.runner.run(...)` returns N candidates → `_apply_post_filters` applied → `WorkingState.candidates` populated, (b) `price_max=80` filter post-prunes results, (c) `last_results` cache reuse for local-rerank-only critiques (REQ-COMPAT-007), (d) empty post-filter result → `candidates=[]` and routing function returns `respond` (verified in T-003), (e) `runner.run` raises → empty delta + log (REQ-AGENT-007). GREEN: import `pipeline.runner.run` + `recommendation._apply_post_filters` (or its public wrapper). REFACTOR: keep node body under 40 lines; lift any helper into `app/channels/recommendation.py` if needed (preserving REQ-MIGR-005 zero-semantic-change). | REQ-AGENT-004 (row 7), REQ-AGENT-007, REQ-COMPAT-005, REQ-COMPAT-007, REQ-MIGR-005 | T-002 | `app/graphs/nodes/search.py`, `tests/test_graph_nodes/test_search_node.py` | pending |
| T-011 | Node 8/10 — `app/graphs/nodes/send_results.py`. RED: `tests/test_graph_nodes/test_send_results.py` covers (a) cards dispatched via fake adapter, (b) `last_results` cached in `SessionStore`, (c) `shown_product_ids` accumulator updated (REQ-COMPAT-006), (d) `WorkingState.sent_candidates` populated, (e) adapter raises → empty delta + log (REQ-AGENT-007). GREEN: lift card-render logic from `scenario.handle_intent_reply`. REFACTOR: extract `_build_card_with_critique_buttons(candidate, idx)` as a module-level helper. | REQ-AGENT-004 (row 8), REQ-AGENT-007, REQ-COMPAT-006, REQ-COMPAT-007 | T-002 | `app/graphs/nodes/send_results.py`, `tests/test_graph_nodes/test_send_results.py` | pending |
| T-012 | Node 9/10 — `app/graphs/nodes/taste_update.py`. RED: `tests/test_graph_nodes/test_taste_update.py` covers (a) `crit:love` callback → `taste_profile.reinforce_more` called, (b) `RoutedIntent.taste_update` from router → `reinforce_*` family invoked with the right signal, (c) `taste_profile.reinforce_*` raises → empty delta + log (REQ-AGENT-007). GREEN: import `taste_profile.reinforce_*` + `user_key_for`. REFACTOR: extract the `signal → reinforce_fn` dispatch table into a module-level dict. | REQ-AGENT-004 (row 9), REQ-AGENT-007, REQ-COMPAT-003, REQ-MIGR-005 | T-002 | `app/graphs/nodes/taste_update.py`, `tests/test_graph_nodes/test_taste_update.py` | pending |
| T-013 | Node 10/10 — `app/graphs/nodes/respond.py`. RED: `tests/test_graph_nodes/test_respond.py` covers (a) success: stub `ChatOpenAI.ainvoke` returns `"Here are some cheaper picks"` and `send_text` dispatched once, (b) `messages` (with up to 3 `SystemMessage` from Q4) is passed in the prompt, (c) `presearch_summary` from Q6 included in prompt context, (d) LLM raises → flow-specific fallback dispatched: empty-search → ZERO_RESULT, link-fail → LINK_FAIL, off-topic → polite nudge, taste-only → TASTE_ACK_TMPL (REQ-LLM-004), (e) output respects `RESPONSE_MAX_TOKENS`, (f) `WorkingState.response_text` populated. GREEN: instantiate module-level `ChatOpenAI` per Q2 (`temperature=0.7`, `max_tokens=settings.RESPONSE_MAX_TOKENS`). REFACTOR: extract per-flow fallback strings into a `_FALLBACKS` dict keyed by a coarse flow tag derived from `WorkingState`. | REQ-AGENT-004 (row 10), REQ-AGENT-006, REQ-AGENT-007, REQ-LLM-002, REQ-LLM-003, REQ-LLM-004 | T-002, T-009, T-010, T-011 | `app/graphs/nodes/respond.py`, `tests/test_graph_nodes/test_respond.py` | pending |
| T-014 | `app/graphs/fashion_bot.py` — graph build + module-level compile cache + Langfuse trace metadata builder. RED: `tests/test_graph_fashion_bot.py` covers (a) `build_graph()` returns a compiled graph, (b) module-level `GRAPH` is a compiled graph, (c) `build_graph() is not GRAPH` (separate instances for test isolation, Q3), (d) `build_metadata(state) -> dict` returns the four REQ-OBSV-003 keys, (e) `build_callback_handler(...)` returns `None` when `LANGFUSE_*` keys are absent and a `CallbackHandler` instance otherwise. GREEN: wire all 10 nodes + 6 routing functions. Compile without checkpointer (REQ-AGENT-008). Extend `app/observability/langfuse.py` with `build_callback_handler(trace_id, session_id, user_id) -> CallbackHandler | None` factory. REFACTOR: pull metadata-builder into `app/observability/langfuse.py` so the webhook + tests share one source of truth. | REQ-AGENT-001, REQ-AGENT-005, REQ-AGENT-008, REQ-OBSV-001, REQ-OBSV-002, REQ-OBSV-003, REQ-OBSV-005 | T-003 through T-013 | `app/graphs/fashion_bot.py`, `app/observability/langfuse.py` (extended), `tests/test_graph_fashion_bot.py` | pending |
| T-015 | Webhook caller swap. RED: `tests/test_webhook_telegram.py` covers (a) HTTP 200 preserved, (b) HTTP 401 on bad secret preserved (REQ-COMPAT-009), (c) `await GRAPH.ainvoke(input_state, config={"callbacks": [handler] if handler else []})` invoked exactly once per webhook, (d) `OutputState.final_state` written to `SessionStore`, (e) `chat_id` SHA-256 prefix used as `session_id` in the callback handler config (REQ-OBSV-005), (f) two consecutive webhooks → two distinct trace starts (REQ-AGENT-008). GREEN: replace `scenario.handle(...)` call in `app/api/webhooks/telegram.py` with graph invocation. REFACTOR: extract `_build_input_state(message, chat_id, from_user_id)` helper colocated with the webhook handler (not in `state.py` — `state.py` stays Pydantic-only). | REQ-AGENT-008, REQ-MIGR-004, REQ-OBSV-001, REQ-OBSV-002, REQ-OBSV-005, REQ-COMPAT-009 | T-014 | `app/api/webhooks/telegram.py`, `tests/test_webhook_telegram.py` (new) | pending |
| T-016 | Delete legacy modules + tests. RED: existence test in `tests/test_graph_flows.py::test_scenario_module_deleted` asserts `import app.channels.scenario` raises `ModuleNotFoundError`. GREEN: `git rm app/channels/scenario.py tests/test_scenario.py`. REFACTOR: scan `app/` for any straggling `from app.channels import scenario` (should be zero after T-015 lands). | REQ-MIGR-001, REQ-MIGR-002 | T-015 | (deletions) `app/channels/scenario.py`, `tests/test_scenario.py` | pending |
| T-017 | Graph-level integration tests in `tests/test_graph_flows.py`. RED+GREEN combined per scenario; one test per row of Section 3's 1:1 mapping table plus the 9 reachable terminal flows from REQ-COMPAT-004. Every test docstring references the REQ it covers (REQ-MIGR-003 acceptance #4). REFACTOR: extract the `FakeAdapter`, `StubPort`, and `FakeCandidate` test doubles from the old `tests/test_scenario.py` into `tests/conftest_graph.py` so per-node tests (T-004 through T-013) can reuse them. | REQ-MIGR-003, REQ-COMPAT-001 through REQ-COMPAT-009 | T-014, T-016 | `tests/test_graph_flows.py`, `tests/conftest_graph.py` | pending |
| T-018 | Per-node unit-test directory finalization. By this point T-004 through T-013 have produced one test file per node. RED: a meta-test `tests/test_graph_nodes/test_inventory.py::test_one_test_file_per_node` asserts `len(glob('tests/test_graph_nodes/test_*.py')) == 11` (10 nodes + routing). GREEN: ensure inventory matches. REFACTOR: nothing — cleanup task. | REQ-AGENT-004 (acceptance #4), REQ-MIGR-002 | T-004 through T-013 | `tests/test_graph_nodes/test_inventory.py` | pending |
| T-019 | Topology assertion test (Q5). RED: `tests/test_graph_topology.py::test_topology_edges_match_spec` per the sketch in Q5. GREEN: lands when the test passes against the real `build_graph()`. REFACTOR: pull magic node-name constants into a module-level frozenset shared with `fashion_bot.py` if duplication grows. | REQ-AGENT-005 (acceptance #9), REQ-AGENT-006 (structural assertion) | T-014 | `tests/test_graph_topology.py` | pending |
| T-020 | Final verification + docs. RED: `ruff check . && ruff format --check .` and `pytest -q` both green. GREEN: append a CHANGELOG note (`feat(agent): SPEC-AGENT-001 LangGraph migration`); add a 1-paragraph README pointer to `app/graphs/`. Verify `grep -R "^from langgraph" app/` returns hits only inside `app/graphs/` (REQ-AGENT-001 acceptance #3) and `grep -R "^from langchain" app/` returns hits only in `app/graphs/nodes/respond.py`, `app/graphs/nodes/ask_clarify.py`, and `app/graphs/state.py` (REQ-AGENT-002 acceptance #2). REFACTOR: none — verification only. | All REQs (Definition of Done) | T-019 | `README.md`, `CHANGELOG.md` (or in-PR description) | pending |

**Total: 20 tasks** (within the SPEC's "Maximum 10 tasks per SPEC" guidance is
exceeded because this is a foundation task — the SPEC explicitly lists 10
nodes + routing + state + caller swap + cleanup, and each is one TDD cycle.
Compressing to 10 would force multi-node tasks that cannot complete in a
single RED-GREEN-REFACTOR cycle).

---

## Section 3 — 1:1 Test Mapping Table (REQ-MIGR-001 PR description requirement)

The current `tests/test_scenario.py` has 14 test functions. Each maps to a new
counterpart in `tests/test_graph_flows.py`. The 9 reachable terminal flows
required by REQ-COMPAT-004 are also enumerated; some overlap with the existing
14 tests (and are noted), others are net-new.

### 3.1 Existing `tests/test_scenario.py` tests → new `tests/test_graph_flows.py` tests

| Old test (test_scenario.py) | New test (test_graph_flows.py) | REQ covered | Notes |
|---|---|---|---|
| `test_direct_photo_upload_is_blocked_before_vision` | `test_direct_photo_blocked_routes_to_respond` | REQ-COMPAT-004 (no silent dead end) | Photo bytes → `ingest` → `respond` (PHOTO_DIRECT_NOT_SUPPORTED fallback). |
| `test_vision_fallback_triggers_zero_result` | `test_vision_fallback_routes_to_respond_zero_result` | REQ-COMPAT-004, REQ-AGENT-006 | `vision_node` returns empty → routing → `respond`. |
| `test_invalid_pick_text_resends_picker` | `test_invalid_pick_text_resends_picker_then_ends` | REQ-AGENT-010, REQ-COMPAT-004 | Free text in `AWAITING_ITEM_PICK` → `pick_item` re-renders carousel → END. |
| `test_intent_reply_runs_search_with_start_msg_and_heartbeat` | `test_intent_reply_full_search_path` | REQ-COMPAT-004, REQ-COMPAT-005 | Heartbeat removed from graph version (not a graph-level concern; if needed, restored as a side effect inside `search_node`). |
| `test_zero_card_render_falls_back_to_text_list` | `test_zero_card_render_routes_to_respond_with_links` | REQ-COMPAT-004, REQ-AGENT-007 | Adapter `send_card` returns False → `send_results` writes a log event and routes to `respond` with a "here are the links" fallback. |
| `test_results_sent_text_triggers_refine_search` | `test_text_in_results_sent_triggers_critique_path` | REQ-COMPAT-002, REQ-COMPAT-005 | `ingest → router_text_decision → critique_apply → search_node → send_results → respond`. |
| `test_refine_without_prior_context_falls_back_to_nudge` | `test_refine_without_context_routes_to_respond_nudge` | REQ-COMPAT-004 | `RoutedIntent.new_search_request` with no `last_results` → `respond`. |
| `test_critique_tap_more_reinforces_taste_and_reruns` | `test_critique_tap_more_updates_taste_and_reruns` | REQ-COMPAT-001, REQ-COMPAT-003 | `crit:more` → `critique_apply` (writes delta + presearch_summary) → `search_node` → `send_results` → `respond`. Taste reinforcement is a side effect inside `critique_apply` for `crit:more` per current `scenario.py` behavior. |
| `test_critique_tap_less_excludes_brand_and_excludes_shown` | `test_critique_tap_less_excludes_brand` | REQ-COMPAT-001, REQ-COMPAT-006 | `crit:less` → `critique_apply` (excludes brand_x) → `search_node` (also excludes shown_product_ids). |
| `test_critique_tap_cheap_sets_max_price` | `test_critique_tap_cheap_sets_price_max` | REQ-COMPAT-001, REQ-COMPAT-005 | `crit:cheap` → `critique_apply` → `_apply_post_filters` enforces `price <= anchor*0.7`. |
| `test_critique_tap_invalid_idx_sends_toast_and_skips_search` | `test_critique_tap_invalid_idx_skips_search` | REQ-COMPAT-001, REQ-AGENT-007 | `parse_callback` returns `None` for malformed payload → `critique_apply` returns empty delta → routing to `respond` (skipping `search_node`). |
| `test_critique_tap_emits_presearch_summary` | `test_critique_tap_emits_presearch_summary` | Q6, REQ-OBSV-003 | Asserts `WorkingState.presearch_summary` is populated by `critique_apply`. |
| `test_sent_cards_carry_critique_buttons` | `test_send_results_attaches_critique_buttons` | REQ-COMPAT-001 | Inline-keyboard buttons on each card carry `crit:*` callback_data. |
| `test_session_caches_results_and_accumulates_shown_ids` | `test_session_caches_last_results_and_shown_ids` | REQ-COMPAT-006, REQ-COMPAT-007 | After `send_results`, `SessionStore.last_results` populated and `shown_product_ids` grows monotonically. |

### 3.2 Net-new tests required by REQ-COMPAT-004 (9 reachable terminal flows)

The SPEC's REQ-COMPAT-004 acceptance #1 mandates an "exhaustive test matrix
over the 9 reachable terminal flows". Mapped below; some are already covered
by 3.1 (noted with a `← see 3.1` pointer):

| # | Terminal flow | New test (test_graph_flows.py) | Already in 3.1? |
|---|---|---|---|
| 1 | link-resolver failure | `test_link_fail_routes_to_respond_link_fail_copy` | net-new |
| 2 | vision-extract failure | `test_vision_fallback_routes_to_respond_zero_result` | ← see 3.1 (`test_vision_fallback_triggers_zero_result`) |
| 3 | vision-empty (no items detected) | `test_vision_empty_routes_to_ask_clarify` | net-new |
| 4 | multi-item picker sent only (END before respond) | `test_multi_item_sends_picker_and_ends` | net-new (REQ-AGENT-010) |
| 5 | weak-vision triggers ask_clarify | `test_weak_vision_routes_to_ask_clarify` | net-new (REQ-AGENT-009) |
| 6 | search returned empty results | `test_search_empty_routes_to_respond` | net-new |
| 7 | search returned results (happy path) | `test_intent_reply_full_search_path` | ← see 3.1 |
| 8 | taste-only update (no new search) | `test_taste_only_update_routes_to_respond_ack` | net-new (REQ-COMPAT-003) |
| 9 | off-topic text in RESULTS_SENT | `test_off_topic_in_results_sent_routes_to_respond` | net-new (REQ-COMPAT-004 PR #10 fix regression test) |

### 3.3 Aggregate

- 14 (mapped from old) + 6 (net-new for REQ-COMPAT-004) = **20 integration
  tests** in `tests/test_graph_flows.py`.
- Plus per-node tests: roughly 6 tests/node × 10 nodes + 18 routing branches
  = **~78 unit tests** across `tests/test_graph_nodes/`.

---

## Section 4 — Risk-to-Test Matrix

The SPEC enumerates 12 risks (R1-R12) in "Risks & Mitigations". For each, the
concrete test exercising the mitigation:

| Risk | Description (short) | Mitigation test |
|---|---|---|
| R1 | LangGraph 1.x API drift | `tests/test_config.py::test_dependency_pin_ranges` asserts `pyproject.toml` declares the documented version ranges (T-001). |
| R2 | Behavior regression vs PR #10 | All 14 mapped tests in 3.1 + REQ-COMPAT-001..009 acceptance tests in `tests/test_graph_flows.py` (T-017). |
| R3 | `respond` introduces new LLM cost | `tests/test_graph_nodes/test_respond.py::test_respect_response_max_tokens` (T-013) + Langfuse cost-metadata assertion in `tests/test_graph_fashion_bot.py::test_metadata_includes_cost_fields` (T-014). |
| R4 | `respond` LLM latency tail | `tests/test_graph_nodes/test_respond.py::test_response_timeout_falls_back_to_template` asserts `RESPONSE_TIMEOUT_MS` honored and fallback dispatched (T-013, REQ-LLM-004). |
| R5 | Langfuse `CallbackHandler` trace nesting wrong | `tests/test_graph_fashion_bot.py::test_callback_handler_nests_under_observe_trace` against a Langfuse mock asserts root → node → generation parent-child (T-014, REQ-OBSV-002). |
| R6 | Two LLM call paths coexist | Existing `tests/test_router.py`, `tests/test_critique.py`, `tests/test_enhance_query.py` continue to hit `LLMProvider.chat`; `tests/test_graph_nodes/test_respond.py` + `test_ask_clarify.py` hit `ChatOpenAI`. Coexistence verified by lint check `grep -R "^from langchain" app/` in T-020. |
| R7 | Compiled graph cached at module import leaks state across tests | `tests/test_graph_fashion_bot.py::test_build_graph_returns_independent_instance` asserts `build_graph() is not GRAPH` (T-014, Q3). |
| R8 | Big-bang rollout with no fallback | **Exempt — manual mitigation**: documented `git revert` procedure in PR description (Section 5 below). No automated test. |
| R9 | Node implicit dep on ContextVar binding | `tests/test_graph_nodes/test_send_results.py::test_unbound_session_store_raises_clear_error` asserts a node called outside a bound ContextVar raises a clear error (T-011, R9 mitigation per SPEC). |
| R10 | `messages` reducer accumulates unboundedly | `tests/test_graph_state.py::test_messages_reducer_caps_at_three_within_one_webhook` (T-002, Q4). |
| R11 | `langfuse[langchain]` extra vs standalone | Resolved by Q1 (no extra needed for v2). `tests/test_observability_langfuse.py::test_callback_handler_imports_from_langfuse_callback` asserts the import path (T-001 / T-014). |
| R12 | Test-file deletion loses historical coverage | Section 3.1 above is the 1:1 mapping; the PR description carries the table. No automated test. |

---

## Section 5 — Worktree + Branch Strategy

- **Branch**: `feature/SPEC-AGENT-001`. Created from `dev` at the start of T-001.
- **Worktree**: Single MoAI worktree at `~/.moai/worktrees/portal-ai/SPEC-AGENT-001/`
  (per `.claude/rules/moai/workflow/worktree-integration.md`). No Claude Native
  worktree (`.claude/worktrees/`) needed because no parallel agent fan-out is
  planned for this SPEC — `manager-tdd` runs the 20 tasks sequentially.
- **PR**: Single PR titled `feat(agent): SPEC-AGENT-001 LangGraph migration`.
  Per REQ-MIGR-001, this PR contains:
  1. New `app/graphs/` directory (T-002 through T-014).
  2. Deletion of `app/channels/scenario.py` and `tests/test_scenario.py` (T-016).
  3. Caller swap in `app/api/webhooks/telegram.py` (T-015).
  4. New `tests/test_graph_nodes/` directory + `tests/test_graph_flows.py` (T-004 through T-018).
  5. **`pyproject.toml` and `uv.lock` updates land in this same PR** (T-001) —
     no separate dependency PR. The SPEC explicitly mandates Big Bang.
  6. `app/core/config.py` + `.env.example` updates for the 5 env vars (T-001).
  7. `app/observability/langfuse.py` extended with `build_callback_handler(...)`
     (T-014) — extension only, no API break.
- **PR description carries**:
  - REQ-COMPAT-* preservation checklist (one line per REQ from REQ-COMPAT-001 to REQ-COMPAT-009).
  - The 1:1 test mapping table from Section 3.1 above.
  - The risk-to-test matrix from Section 4 above.
  - Revert procedure: `git revert <merge-commit-sha>` reverts the entire PR
    atomically; the graph and the deleted scenario module move together. No
    schema migrations to undo, no env-var deprecation flow needed.
- **No feature flag** (REQ-MIGR-001). No `MESSENGER_BACKEND`-style switch.

---

## Appendix A — Reference: seed-lognia patterns adopted

(From SPEC `.moai/specs/SPEC-AGENT-001/spec.md` Cross-References section.)

`~/Desktop/seed-lognia/app/graphs/agentic_rag.py` is accessible on this
machine. The patterns we adopt:

- Pydantic state with `add_messages` reducer.
- `add_conditional_edges` + dict mapping for routing (we keep separate
  `_route_after_*` functions per Q5 for explicit-test friendliness).
- `StateGraph(...).compile()` cached at module level (Q3).
- ContextVar isolation pattern (already in use in `app/channels/session.py`
  and `app/channels/recommendation.py` — REQ-STATE-005).

The patterns we explicitly avoid (per SPEC Cross-References):

- Single 1500-line graph file → we split into `app/graphs/nodes/*.py` (one file
  per node, REQ-AGENT-004 acceptance #1).
- Class-method nodes → we use module-level `async def` functions (REQ-AGENT-004
  acceptance #2).
- Single 20-field state → we use the 3-tier Input/Working/Output split (REQ-STATE-001).
- Mixin pattern → we compose via imported helpers from `app/channels/*.py`
  (REQ-MIGR-005).

---

## Appendix B — Notes for `manager-tdd`

- **TDD cycle per task**: RED test name is included in each task description
  in Section 2. Write the test, see it fail, write the minimum code to pass,
  refactor, commit. Conventional commit format: `feat(agent): T-XXX <short
  description>` for GREEN commits, `test(agent): T-XXX <short description>`
  for RED commits, `refactor(agent): T-XXX <short description>` for REFACTOR
  commits. Final task T-020 lands the PR-ready state.
- **Coverage target**: `quality.yaml` default. Per REQ-MIGR-003 acceptance #3,
  `app/graphs/**` coverage must be at least the project default.
- **`ruff` and `pytest`**: T-020 is the gate. Per-task incremental ruff is
  recommended (each commit should leave the tree green) but not formally
  enforced until T-020.
- **Out of scope for `manager-tdd`**: anything in the SPEC's "Exclusions"
  section. If a task's RED test starts to drift toward an excluded behavior
  (e.g. "test for AGENT_GRAPH_ENABLED feature flag"), STOP and report a
  blocker per `agent-common-protocol.md` "Surface Assumptions [HARD]".
