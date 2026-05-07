---
id: SPEC-AGENTIC-CRITIQUE-001
version: 0.1.0
status: draft
created: 2026-05-07
updated: 2026-05-07
author: hchsa77@gmail.com
priority: P1
issue_number: null
---

# SPEC-AGENTIC-CRITIQUE-001: Self-Critique Loop for Telegram Bot Search Pipeline

## HISTORY

- 2026-05-07 (v0.1.0): Initial draft. Roadmap item A2 — agent observes its own
  search output, generates self-feedback, and retries with a refined query
  (Reflexion pattern). Builds on SPEC-AGENT-001 (LangGraph 10-node topology —
  extended with one new `evaluator` node and a retry edge), SPEC-VISION-UNIFY-001
  (rich Vision schema feeds the evaluator's reasoning context), and
  SPEC-PIPELINE-001 (search pipeline being looped — RPC and scoring are
  unchanged; only the loop around it is added). Triggered by an observed
  real-world incident where `search_products_v5` returned `raw_count=0` for a
  perfectly clear "blue jeans" image and the bot shipped that empty result back
  to the user with no retry.

---

## Goal

The Telegram bot's LangGraph today runs a **one-shot** search pipeline:

```
ingest → vision → pick_item → critique_apply → search → send_results → respond
```

The bot has **no mechanism to evaluate its own search results**. If
`search_node` returns 0 candidates, or returns candidates that don't match the
user's intent (wrong subcategory, wrong fit, wrong color family), the user
either has to manually `crit:less` / `crit:more` from the carousel buttons or
send a free-text follow-up to fix it. We just observed a real case where the
RPC returned `raw_count=0` for a clear "blue jeans" image — and the bot just
shipped that empty result back to the user with no internal retry, no broaden,
no reflection.

This SPEC inserts an `evaluator` node between `search_node` and `send_results`
that implements the classic **Reflexion** pattern from agentic systems
literature:

1. The evaluator reads the candidates plus search context (vision item, user
   intent, taste profile) and asks an LLM "do these results actually match the
   user's intent? score 0.0–1.0 + reasoning".
2. If the score is at or above the configured threshold (default 0.6), OR the
   retry budget is exhausted, the system proceeds to `send_results`.
3. If the score is below threshold and budget remains, the evaluator produces
   a **synthesized `critique_delta`** describing how to refine the query
   (broaden, specify color, drop a fit constraint, exclude brands), and the
   graph routes back to `search_node` with the delta applied.
4. A hard cap of 2 retry iterations (configurable via
   `SELF_CRITIQUE_MAX_ITERATIONS`) prevents runaway loops.
5. As a cost optimization, the **empty-results fast-path** (0 candidates on
   the first iteration) synthesizes a "broaden the query" delta WITHOUT an
   LLM call — the most common failure mode does not pay the evaluator-LLM tax.

This is **internal-only self-critique** — distinct from the existing
**user-driven** `crit:more` / `crit:less` callback flow handled by
`critique_apply`, which remains unchanged. The user's explicit signal continues
to trump self-critique.

The migration is **graph-topology + state plumbing only**: no new product
capabilities, no new search RPC, no new UI affordances. The acceptance bar is
that empty results retry once with a broadened query, that low-quality results
retry with a refined query (when LLM judgment fires), and that the loop
terminates safely under all conditions.

This SPEC describes WHAT the new node, retry contract, and safety guards must
do. Exact LLM prompt text, file structure, schema for `CritiqueDelta`, and
rollout sequencing belong in `plan.md`.

## Non-Goals

- **Replacing SPEC-MSG-001.** Channel transport (Telegram adapter, webhook
  parser, secret-token verification) is reused unchanged.
- **Replacing SPEC-AGENT-001.** The 10-node LangGraph topology is extended
  by exactly one new node (`evaluator`) and one new conditional retry edge
  (`evaluator → search_node`). No node is removed; no node's contract is
  rewritten beyond `search_node` accepting an applied delta on retry and
  `respond` reading a new `critique_exhausted` hint.
- **Replacing SPEC-VISION-UNIFY-001.** The rich Vision schema is consumed
  by the evaluator as read-only context. No Vision schema change.
- **Replacing SPEC-PIPELINE-001.** The `embed → enhance_query → search →
  diversify` pipeline runs unchanged. The evaluator wraps it; it does not
  modify it.
- **Changing the user-facing `crit:more` / `crit:less` callback flow.**
  `app/graphs/nodes/critique_apply.py` is unchanged. User-driven critique
  is explicit, immediate, and resets retry state — distinct from internal
  self-critique.
- **Introducing episodic memory (B4) or onboarding interview (B6).** Those
  are separate roadmap items and do not interact with self-critique.
- **Vision-based evaluation of candidates.** v1 evaluator runs on text
  metadata only (product titles, brands, categories, tags). Image-based
  evaluation is deferred (cost-prohibitive at current volume).
- **Streaming evaluator output, multi-turn critique conversation, or
  evaluator tool-calling.** Single-shot JSON-extraction LLM call, same shape
  as `vision.extract`.
- **Production rollout strategy beyond a feature flag.** This server is
  dev-only; the flag is a toggle, not a canary mechanism.

## Stakeholders

| Role | Responsibility |
|------|----------------|
| Product / Founder (hchsa77@gmail.com) | Approves the score threshold (default 0.6), the retry cap (default 2), and the "user-perceived latency vs result quality" tradeoff. Sign-off on REQ-CRITIQUE-RETRY-* and REQ-CRITIQUE-COST-*. |
| AI Server Owner (this SPEC) | All work in `app/graphs/nodes/evaluator.py` (NEW), `app/graphs/nodes/evaluator_prompt.py` (NEW), `app/graphs/state.py` (state extension), `app/graphs/routing.py` (new edge function), `app/graphs/fashion_bot.py` (graph wiring), `app/graphs/nodes/search.py` (delta application contract), `app/graphs/nodes/respond.py` (read `critique_exhausted` hint), `app/core/config.py` (new env vars). Owns evaluator tests, retry-loop tests, and safety-guard tests. |
| Langfuse Operator | Verifies the new `evaluator` span carries the documented metadata (score, reasoning, suggested_delta_summary, retry_count). Verifies the existing observability budget is not blown by the additional spans. |
| Modal / Supabase Teams | Out of scope — embeddings and RPC unchanged. The retry adds one additional `search_products_v5` call per retry iteration; capacity headroom verified independently. |

---

## Architecture Snapshot (informative)

Today (one-shot):

```
ingest → resolve_image → vision → pick_item → ask_clarify? → critique_apply →
  search → send_results → taste_update → respond
```

After this SPEC (with self-critique loop):

```
ingest → resolve_image → vision → pick_item → ask_clarify? → critique_apply →
  search → evaluator ─┐
                      │
              ┌───────┴───────┐
              │ score >= thr  │ score < thr AND budget remains
              ▼               ▼
       send_results      apply delta → search (retry)
              │               │
              │               ▼  (budget exhausted: ship last results)
              ▼          send_results (with critique_exhausted=true)
       taste_update           │
              │               ▼
              └─────────► respond
```

**Affected modules in portal/ai (this SPEC)**:

- `app/graphs/nodes/evaluator.py` — NEW. The evaluator node.
- `app/graphs/nodes/evaluator_prompt.py` — NEW. The evaluator's system /
  user prompt strings, mirrors the `vision_prompt.py` pattern from
  SPEC-VISION-UNIFY-001.
- `app/graphs/state.py` — `WorkingState` extended with
  `critique_retry_count`, `critique_trail`, `critique_exhausted`,
  `critique_pending_delta`. `OutputState` extended with `critique_exhausted`
  for `respond` consumption.
- `app/graphs/routing.py` — new conditional edge function
  `after_evaluator(state) → "search_node" | "send_results"`.
- `app/graphs/fashion_bot.py` — graph wiring updated: a new node and
  conditional retry edge added; existing `after_search` is repointed at
  `evaluator`.
- `app/graphs/nodes/search.py` — `_build_request` extended to consume
  `critique_pending_delta` and apply it to the request fields when
  populated. Existing single-shot path preserved when delta is `None`.
- `app/graphs/nodes/respond.py` — reads `OutputState.critique_exhausted`
  and softens the natural-language reply when `True`.
- `app/observability/langfuse.py` — no API change; the new node uses the
  same `@observe` decoration pattern, and `build_callback_handler` covers
  it automatically.
- `app/core/config.py` and `.env.example` — new env vars (see "Environment
  Variables" section).
- `tests/test_graph_nodes/test_evaluator.py` (NEW),
  `tests/test_graph_nodes/test_search.py` (extended for delta application),
  `tests/test_graph_flows.py` (extended for full retry-loop scenarios),
  `tests/test_graph_safety.py` (NEW — budget exhaustion + stagnation +
  timeout guard tests).

**Reused, untouched modules**:

- `app/channels/telegram/*`, `app/channels/adapter.py`,
  `app/channels/factory.py`, `app/channels/critique.py` (user-driven
  critique helper), `app/channels/router.py`, `app/channels/taste_profile.py`,
  `app/channels/link_resolver.py`, `app/channels/vision.py`,
  `app/channels/vision_prompt.py`, `app/channels/session.py` — channel
  surface unchanged.
- `app/pipeline/runner.py`, `app/pipeline/embed.py`,
  `app/pipeline/enhance_query.py`, `app/pipeline/search.py`,
  `app/pipeline/diversify.py` — search pipeline unchanged.
- `app/providers/llm.py`, `app/providers/embedding.py`,
  `app/providers/database.py` — providers unchanged.
- `app/api/recommend.py`, `app/api/health.py`,
  `app/api/webhooks/telegram.py` — API surface unchanged.
- `app/graphs/nodes/critique_apply.py` — user-driven critique unchanged.

---

## Schema Reference (informative — formalized in REQ-CRITIQUE-EVAL-002)

### `CritiqueScore` (evaluator output contract)

| Field | Type | Notes |
|-------|------|-------|
| `score` | `float` | Range `[0.0, 1.0]`. `1.0` = candidates fully match user's intent; `0.0` = candidates are useless. |
| `reasoning` | `str` | Short justification (≤ 200 chars). Used for Langfuse metadata + log line. Never surfaced to user. |
| `suggested_delta` | `CritiqueDelta \| None` | Populated when `retry=True`. The refinement to apply on the next search iteration. |
| `retry` | `bool` | Final routing decision. `True` → loop back to `search_node` (subject to budget guard). `False` → proceed to `send_results`. |

### `CritiqueDelta` (refinement description)

The exact set of fields is decided in `plan.md` (Open Question 5), but at
minimum it SHALL cover the same axes as the existing
`crit:more` / `crit:less` user-critique callback so that the two flows can
share downstream wiring. The expected fields:

| Field | Type | Source intent |
|-------|------|---------------|
| `intent` | `str` | One of `"broaden"`, `"narrow"`, `"refine_color"`, `"refine_fit"`, `"exclude_brands"`, `"exclude_keywords"`. |
| `boost_keywords` | `list[str]` | Keywords to add to the sparse query. |
| `exclude_keywords` | `list[str]` | Keywords to exclude. |
| `exclude_brands` | `list[str]` | Brand names to exclude (matched against candidate metadata). |
| `color_override` | `str \| None` | Replaces `colorFamily` for the next search. |
| `fit_override` | `str \| None` | Replaces `fit` for the next search. |
| `drop_min_price` | `bool` | Drop the `min_price` filter (broaden only). |
| `drop_max_price` | `bool` | Drop the `max_price` filter (broaden only). |
| `drop_filters` | `list[str]` | Free-form list of filter names to drop on the next search (broaden only). |

The Pydantic models SHALL forbid extra fields (`ConfigDict(extra="forbid")`)
so that drift between the evaluator's prompt and the consumer (`search_node`)
fails fast.

---

## Requirements (EARS)

### Evaluator Node (REQ-CRITIQUE-EVAL-*)

#### REQ-CRITIQUE-EVAL-001 — `evaluator` node SHALL be invoked between `search_node` and `send_results` [P0]

**WHEN** `search_node` produces candidates (whether non-empty or empty),
**THE SYSTEM SHALL** invoke an `evaluator` node before `send_results`.

**Acceptance criteria**:

- A new node `app/graphs/nodes/evaluator.py` is registered in
  `app/graphs/fashion_bot.py` between `search_node` and `send_results`.
- The existing edge `search_node → send_results` is replaced with
  `search_node → evaluator`, and `evaluator` has TWO outgoing edges via
  the new `after_evaluator` conditional:
  - `evaluator → search_node` (retry path), and
  - `evaluator → send_results` (exit path).
- A unit test on the wired graph asserts `evaluator` is in the topology and
  is reachable from `search_node` on every search-completion path.
- The evaluator node SHALL be idempotent on its read-only inputs and SHALL
  NOT mutate any state other than the new `WorkingState` fields documented
  in REQ-CRITIQUE-RETRY-002 and REQ-CRITIQUE-RETRY-003.

#### REQ-CRITIQUE-EVAL-002 — Evaluator SHALL emit a typed `CritiqueScore` Pydantic model [P0]

**THE SYSTEM SHALL** define `CritiqueScore` and `CritiqueDelta` as Pydantic
v2 `BaseModel` classes with `model_config = ConfigDict(extra="forbid",
str_strip_whitespace=True)`. The evaluator node SHALL return a
`CritiqueScore` populated as documented in the "Schema Reference" section.

**Acceptance criteria**:

- `CritiqueScore` and `CritiqueDelta` live in `app/graphs/nodes/evaluator.py`
  (or a sibling `app/graphs/nodes/evaluator_models.py` — `plan.md` chooses).
- A unit test instantiates `CritiqueScore(score=0.4, reasoning="too narrow",
  suggested_delta=CritiqueDelta(intent="broaden", drop_filters=["min_price"]),
  retry=True)` and asserts construction succeeds with `extra="forbid"`
  semantics.
- A unit test asserts `score` is range-validated to `[0.0, 1.0]`
  (Pydantic `Field(ge=0.0, le=1.0)`).
- A unit test asserts that when `retry=False`, `suggested_delta` MAY be
  `None` and the model still validates.
- A unit test asserts that when `retry=True` AND `suggested_delta is None`,
  the evaluator's caller logic treats it as a hard failure and exits the
  loop (defensive — see REQ-CRITIQUE-LOOP-SAFETY-001).

#### REQ-CRITIQUE-EVAL-003 — Evaluator LLM call SHALL use a configurable model and SHALL fail safe [P0]

**THE SYSTEM SHALL** issue the evaluator's LLM call against the existing
LiteLLM proxy via the existing `LLMProvider` (httpx). The model is
configurable via `EVALUATOR_MODEL` (default `gpt-4o-mini`),
`max_tokens` via `EVALUATOR_MAX_TOKENS` (default `400`), `temperature`
via `EVALUATOR_TEMPERATURE` (default `0.2`). On any failure path (timeout,
HTTP error, JSON parse failure, Pydantic validation failure), the evaluator
node SHALL return a synthetic `CritiqueScore(score=1.0, reasoning="evaluator
failed — shipping current results", suggested_delta=None, retry=False)` —
i.e., the original results ship and the user is not penalized for an
evaluator outage.

**Acceptance criteria**:

- `app/core/config.py` declares `EVALUATOR_MODEL: str = "gpt-4o-mini"`,
  `EVALUATOR_MAX_TOKENS: int = 400`, `EVALUATOR_TEMPERATURE: float = 0.2`,
  `EVALUATOR_TIMEOUT_S: float = 8.0`.
- A unit test patches `LLMProvider.chat` to raise `httpx.TimeoutException`
  and asserts the evaluator returns `score=1.0, retry=False` (fail-open).
- A unit test patches the LLM to return malformed JSON and asserts the
  evaluator returns `score=1.0, retry=False` (fail-open).
- A unit test patches the LLM to return `score=1.5` (out of range) and
  asserts that Pydantic validation fails and the evaluator returns the
  fail-open synthetic score.
- The synthetic-fail-open score SHALL be logged at `WARNING` level with
  the underlying exception, so on-call has visibility.
- The evaluator's prompt and parameters live in
  `app/graphs/nodes/evaluator_prompt.py` (mirrors the `vision_prompt.py`
  pattern from SPEC-VISION-UNIFY-001).

---

### Retry Loop (REQ-CRITIQUE-RETRY-*)

#### REQ-CRITIQUE-RETRY-001 — Retry SHALL apply the suggested delta and re-invoke `search_node` [P0]

**WHEN** the evaluator reports `retry=True`
**AND** `WorkingState.critique_retry_count < SELF_CRITIQUE_MAX_ITERATIONS`
(default `2`), **THE SYSTEM SHALL** apply `suggested_delta` to the search
request fields and re-invoke `search_node`. The loop SHALL terminate
deterministically after at most `SELF_CRITIQUE_MAX_ITERATIONS` retries.

**Acceptance criteria**:

- `app/graphs/routing.py` exposes `after_evaluator(state) → str` that
  returns `"search_node"` when `retry=True AND critique_retry_count <
  max_iterations` (and other guards pass — REQ-CRITIQUE-LOOP-SAFETY-*),
  else `"send_results"`.
- `app/graphs/nodes/search.py::_build_request` reads
  `WorkingState.critique_pending_delta` and applies it (overrides
  `colorFamily`, `fit`; merges `boost_keywords`, `exclude_keywords`,
  `exclude_brands` into the request; honors `drop_min_price`,
  `drop_max_price`, `drop_filters`). When `critique_pending_delta is None`,
  the existing single-shot path is preserved byte-for-byte.
- A unit test with a fixture `CritiqueScore(retry=True,
  suggested_delta=CritiqueDelta(intent="broaden", drop_filters=["min_price",
  "max_price"]))` asserts that the second `search_node` invocation receives
  a `RecommendRequest` without those filters.
- After the delta is applied, `WorkingState.critique_pending_delta` SHALL
  be cleared back to `None` (single-use semantics) so the next iteration
  starts clean unless the evaluator re-emits a delta.
- `SELF_CRITIQUE_MAX_ITERATIONS` is enforced at the routing-edge level
  (REQ-CRITIQUE-LOOP-SAFETY-001), not inside the evaluator node, so the
  evaluator can stay stateless.

#### REQ-CRITIQUE-RETRY-002 — `WorkingState` SHALL persist retry trail and counters [P0]

**THE SYSTEM SHALL** extend `app/graphs/state.py::WorkingState` with the
following fields:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `critique_retry_count` | `int` | `0` | Increments by 1 every time the loop routes back to `search_node` from `evaluator`. |
| `critique_trail` | `list[dict]` | `[]` | Append-only audit log. One entry per evaluator invocation: `{iteration: int, score: float, reasoning: str, suggested_delta_summary: str | None, candidates_count_in: int, candidates_count_out: int, source: "fast_path" | "llm", elapsed_ms: int}`. |
| `critique_pending_delta` | `CritiqueDelta \| None` | `None` | Populated when the evaluator decides to retry. Cleared by `search_node` after consumption. |
| `critique_exhausted` | `bool` | `False` | Set to `True` ONLY if the loop exited because the budget was exhausted (max iterations OR stagnation OR timeout) AND the final score was below threshold. NOT set when a retry succeeded above threshold. |
| `critique_started_at_ms` | `int \| None` | `None` | Wall-clock monotonic timestamp at first evaluator entry. Used for the timeout guard (REQ-CRITIQUE-LOOP-SAFETY-003). |

**Acceptance criteria**:

- The fields are declared with Pydantic v2 type hints and defaults
  consistent with the rest of `WorkingState` (REQ-STATE-002 from
  SPEC-AGENT-001).
- A unit test instantiates a fresh `WorkingState` and asserts all five
  fields take their declared defaults.
- A unit test runs a 2-iteration loop and asserts:
  - `critique_retry_count == 2` after both retries.
  - `critique_trail` has 3 entries (initial evaluation + 2 retries).
  - `critique_pending_delta is None` after `search_node` consumed it.
- The reducer behavior from SPEC-AGENT-001 REQ-STATE-002 SHALL be
  preserved; this SPEC only adds fields. `critique_trail` uses an
  append-merge reducer (similar to the `messages` reducer pattern).

#### REQ-CRITIQUE-RETRY-003 — Budget exhaustion SHALL ship the last iteration's results with `critique_exhausted=true` [P0]

**WHEN** the retry budget is exhausted (`critique_retry_count >=
SELF_CRITIQUE_MAX_ITERATIONS` OR stagnation OR timeout) AND the final
score is below threshold,
**THE SYSTEM SHALL** route to `send_results` carrying whatever the last
iteration produced (even if 0 candidates), AND SHALL set
`OutputState.critique_exhausted = True` so that `respond` can soften the
natural-language reply.

**Acceptance criteria**:

- A unit test exhausts the budget with synthetic low scores on every
  iteration and asserts the user receives the last iteration's candidate
  set (which may be empty) AND that `respond`'s rendered text reflects the
  softer "couldn't find a perfect match" copy.
- A unit test with a successful early exit (score crosses threshold on
  iteration 1) asserts `critique_exhausted` stays `False` even though
  retries were attempted on prior turns of an unrelated prior session
  (state is per-graph-invocation, not per-session).
- The exact softer-reply copy is decided in `plan.md` (Open Question 8) —
  this SPEC requires only that `respond` reads the flag and produces
  visibly-different copy when it is `True`.
- `OutputState.critique_exhausted` SHALL also be `True` when the loop
  exits because the final iteration returned 0 candidates AND budget is
  exhausted, so the empty-result acknowledgment is consistent with the
  low-quality-result acknowledgment.

---

### Empty-Result Fast-Path (REQ-CRITIQUE-EMPTY-*)

#### REQ-CRITIQUE-EMPTY-001 — Empty results on first iteration SHALL trigger an LLM-free broaden delta [P0]

**WHEN** `search_node` returns 0 candidates **AND**
`WorkingState.critique_retry_count == 0`,
**THE SYSTEM SHALL** synthesize a `CritiqueDelta(intent="broaden",
drop_filters=["min_price", "max_price", "exclude_keywords"], …)` WITHOUT
calling the evaluator LLM, append a `critique_trail` entry with
`source="fast_path"`, and immediately retry. If the retry STILL returns 0
candidates, the next iteration SHALL go through the evaluator LLM as usual
(no second fast-path).

**Acceptance criteria**:

- A unit test with `search_node` mocked to return 0 candidates on
  iteration 0 asserts:
  - The evaluator's LLM was NOT called on iteration 0.
  - `WorkingState.critique_pending_delta.intent == "broaden"`.
  - `WorkingState.critique_pending_delta.drop_filters` includes
    `min_price`, `max_price`, and `exclude_keywords`.
  - `critique_trail[0].source == "fast_path"`.
  - `critique_retry_count == 1` after the retry.
- A unit test with `search_node` mocked to return 0 candidates on BOTH
  iteration 0 and iteration 1 asserts that the evaluator's LLM IS called
  on iteration 1 (the LLM-free fast-path fires only once per turn).
- The exact set of filters dropped on the fast-path is configurable via
  `SELF_CRITIQUE_FASTPATH_DROP_FILTERS` (default
  `min_price,max_price,exclude_keywords`) — `plan.md` may tune this
  default after observing real production patterns.
- The fast-path SHALL preserve `keywords` and the rich Vision-derived
  fields (`searchQueryKo`, `subcategory`, `colorFamily` from
  SPEC-VISION-UNIFY-001) — those carry the user's actual intent and must
  not be dropped.
- A `[CRITIQUE][fast-path]` log line is emitted with the dropped filters
  enumerated for on-call diagnostics.

---

### Loop Safety (REQ-CRITIQUE-LOOP-SAFETY-*)

#### REQ-CRITIQUE-LOOP-SAFETY-001 — Routing edge SHALL hard-cap iteration count [P0]

**THE SYSTEM SHALL** enforce `critique_retry_count <=
SELF_CRITIQUE_MAX_ITERATIONS` at the routing-edge level
(`after_evaluator` conditional). Even if the evaluator misbehaves and
returns `retry=True` with a `suggested_delta` after the budget has been
exhausted, the routing edge SHALL force the exit to `send_results`.

**Acceptance criteria**:

- A unit test with the evaluator mocked to ALWAYS return `retry=True`
  asserts that the loop exits after exactly
  `SELF_CRITIQUE_MAX_ITERATIONS` retries (default 2, so 3 total search
  invocations: initial + 2 retries).
- A unit test with `SELF_CRITIQUE_MAX_ITERATIONS=0` asserts the loop is
  effectively disabled — `evaluator` runs once, but no retry path is
  taken, regardless of score.
- A unit test with `SELF_CRITIQUE_MAX_ITERATIONS=5` (above default)
  asserts the cap raises proportionally.
- The hard cap is enforced in `app/graphs/routing.py::after_evaluator`,
  not in the evaluator node body, so the evaluator stays stateless.

#### REQ-CRITIQUE-LOOP-SAFETY-002 — Stagnation guard SHALL exit on duplicate delta [P0]

**WHEN** the evaluator emits a `suggested_delta` whose semantic content
matches the previous iteration's `suggested_delta`,
**THE SYSTEM SHALL** exit the loop and proceed to `send_results`, even if
budget remains. "Semantic match" is defined as set-equivalence on the
fields `(exclude_brands, exclude_keywords, boost_keywords, color_override,
fit_override, drop_filters, intent)`. Order does not matter; the deltas
are compared as canonical-form `frozenset`s where applicable.

**Acceptance criteria**:

- A unit test with the evaluator mocked to return the SAME
  `suggested_delta` on iterations 1 and 2 asserts the loop exits after
  iteration 2 (the second occurrence) regardless of remaining budget,
  and that `OutputState.critique_exhausted == True`.
- A unit test with a single-iteration loop (no prior delta to compare)
  asserts the stagnation guard does NOT fire spuriously.
- A unit test with `delta_a` then `delta_b` (genuinely different) asserts
  the guard does NOT fire and the loop proceeds normally.
- The set-equivalence comparison SHALL be implemented in a helper
  `app/graphs/nodes/evaluator.py::deltas_equivalent` and SHALL be
  unit-tested independently with at least 6 fixture pairs.
- The guard SHALL also fire when the new iteration's score is LOWER than
  the previous iteration's score (R5 mitigation — score regression
  signals the evaluator is making things worse). This rule is encoded as
  REQ-CRITIQUE-LOOP-SAFETY-002a below for clarity but lives in the same
  routing path.

##### REQ-CRITIQUE-LOOP-SAFETY-002a — Score regression SHALL exit the loop [P0]

**WHEN** the current iteration's score is strictly lower than the previous
iteration's score (`current.score < previous.score`),
**THE SYSTEM SHALL** exit the loop and proceed to `send_results` carrying
the PREVIOUS iteration's results (the higher-scoring set), not the
current one. This prevents a misbehaving evaluator from steering the bot
into worse and worse results.

**Acceptance criteria**:

- A unit test with iteration scores `[0.4, 0.55, 0.30]` asserts the loop
  exits after iteration 2 (score regression: 0.55 → 0.30) and ships the
  iteration-1 results (the 0.55-scoring set).
- A unit test with monotonically-increasing scores `[0.4, 0.55, 0.70]`
  asserts the loop continues normally and exits on iteration 2 because
  the score crossed the 0.6 threshold.
- The "ship previous results" behavior requires `WorkingState` to hold
  the previous iteration's candidate set; the implementation may choose
  to (a) keep `previous_candidates` as a state field, or (b) tie it to
  the `critique_trail` entry — `plan.md` decides.

#### REQ-CRITIQUE-LOOP-SAFETY-003 — Wall-clock timeout SHALL force exit [P0]

**THE SYSTEM SHALL** track wall-clock elapsed time from the FIRST
evaluator entry and SHALL force-exit the loop to `send_results` when the
elapsed time exceeds `SELF_CRITIQUE_TIMEOUT_S` (default `30.0` seconds).

**Acceptance criteria**:

- A unit test mocks `time.monotonic()` to advance past the timeout and
  asserts the loop exits regardless of remaining iteration budget,
  shipping the last-completed iteration's results.
- The timeout cap is independent of (and stricter than) the per-evaluator
  LLM call timeout (`EVALUATOR_TIMEOUT_S`, default `8.0`). The total
  budget is total-loop-bound, not per-call.
- `WorkingState.critique_started_at_ms` is set on the first evaluator
  entry and never overwritten within the same graph invocation.
- A `[CRITIQUE][timeout]` log line is emitted on force-exit with elapsed
  ms and iteration count.
- The 30-second total cap is consistent with the SPEC-MSG-001 12-second
  end-to-end budget being a SOFT target; in practice, the loop should
  exit well before 30s on healthy paths. The 30s cap is a hard ceiling
  for pathological cases (e.g., LiteLLM degraded but not failing).

---

### Cost & Feature Flag (REQ-CRITIQUE-COST-*)

#### REQ-CRITIQUE-COST-001 — Self-critique SHALL be gated by `SELF_CRITIQUE_ENABLED` [P0]

**THE SYSTEM SHALL** introduce a `SELF_CRITIQUE_ENABLED` env var (default
`true`). When set to `false`, the graph SHALL behave exactly as it does
today: `search_node → send_results`, with no `evaluator` invocation, no
retries, no state-field writes for `critique_*`. This provides a one-flip
rollback path and a regression-test toggle.

**Acceptance criteria**:

- The flag lives in `app/core/config.py`
  (`Settings.SELF_CRITIQUE_ENABLED: bool = True`) and in `.env.example`.
- The graph wiring in `app/graphs/fashion_bot.py` SHALL select the
  topology at compile time based on the flag — when `False`, the
  `search_node → send_results` direct edge is restored.
- A unit test parameterized over the flag asserts:
  - `True` (default): `evaluator` node present, retry path reachable,
    `critique_retry_count` field populated.
  - `False`: `evaluator` node absent from the compiled topology,
    `critique_retry_count` remains at default `0`, no `evaluator` LLM
    call is issued.
- The flag is logged at startup in `app/main.py` lifespan so on-call can
  verify it from the boot log.
- All 162 existing tests SHALL pass when `SELF_CRITIQUE_ENABLED=false`
  (REQ-CRITIQUE-COMPAT-002).

#### REQ-CRITIQUE-COST-002 — Per-message cost SHALL be tracked and documented [P0]

**THE SYSTEM SHALL** document the per-message expected cost added by the
self-critique loop, broken down by the typical iteration paths. The
documented baseline (informative — does not change at runtime):

| Path | Evaluator LLM calls | Search RPC calls | Estimated added cost |
|------|---------------------|------------------|----------------------|
| Healthy single-shot (score >= 0.6 on iter 0) | 1 | 0 (initial only) | ~$0.0005 |
| Empty-result fast-path success (broaden retries to non-empty) | 1 (eval after retry) | 1 (one retry) | ~$0.0005 + RPC |
| Full LLM retry (1 retry) | 2 | 1 | ~$0.001 + RPC |
| Full LLM retry (2 retries, exhaustion) | 3 | 2 | ~$0.0015 + 2× RPC |

**Acceptance criteria**:

- The cost table lives verbatim in this SPEC and in `plan.md`'s rollout
  section.
- The Langfuse `evaluator` span carries `model`, `prompt_tokens`,
  `completion_tokens`, `total_tokens` (standard Langfuse-LLM metadata)
  so per-iteration cost is observable post-hoc.
- A weekly Langfuse report (manual, ad-hoc — automation deferred) SHALL
  validate the actual cost against the documented baseline. If actual
  P95 cost exceeds the documented baseline by more than 3× for two
  consecutive weeks, the flag SHALL flip to `False` pending tuning.
- P50 / P95 latency overhead SHALL be tracked in Langfuse on the
  `graph_run` span — expected P50 overhead ≤ 1s (single evaluator call),
  expected P95 overhead ≤ 8s (two retries with LLM evaluator on each).

---

### Observability (REQ-CRITIQUE-OBSV-*)

#### REQ-CRITIQUE-OBSV-001 — `evaluator` Langfuse span SHALL include retry metadata [P0]

**WHEN** the evaluator runs (whether via LLM call or fast-path),
**THE SYSTEM SHALL** include the following metadata on the `evaluator`
Langfuse span:

| Metadata key | Value source |
|--------------|--------------|
| `score` | `CritiqueScore.score` |
| `reasoning` | `CritiqueScore.reasoning` (truncated to 200 chars) |
| `retry` | `CritiqueScore.retry` |
| `retry_count` | `WorkingState.critique_retry_count` AT span start |
| `suggested_delta_summary` | Compact string repr of `CritiqueDelta` (e.g., `"broaden:drop_min_price,drop_max_price"`) — `None` when no delta |
| `candidates_count_in` | Number of candidates the evaluator received |
| `candidates_count_out` | Number of candidates that will be passed downstream after the routing decision |
| `source` | `"fast_path"` or `"llm"` |
| `evaluator_model` | Value of `EVALUATOR_MODEL` env (when `source="llm"`); `None` for fast-path |
| `elapsed_ms` | Wall-clock time inside the evaluator node |

**Acceptance criteria**:

- A unit test against a Langfuse mock asserts the metadata dict on the
  `evaluator` span contains all 10 keys with values derived from a fixture
  invocation.
- No PII (raw `chat_id`, raw `from_user_id`, raw image URL, full product
  titles) appears in any new field — this preserves the SPEC-AGENT-001
  REQ-OBSV-005 invariant.
- Langfuse trace size is monitored; the new keys add ≤ 0.5 KB per
  iteration, ≤ 1.5 KB per turn at full retry depth. No trace-size
  regression test required, but a smoke check on trace bytes is
  documented in `plan.md`.

#### REQ-CRITIQUE-OBSV-002 — Per-iteration logger output SHALL emit a structured CRITIQUE line [P0]

**WHEN** the evaluator completes an iteration,
**THE SYSTEM SHALL** emit one structured INFO log line of the form
`[CRITIQUE][n/N] score=0.42 retry=true source=llm reason="too narrow — drop fit"
candidates_in=2 candidates_out=2 elapsed_ms=812`
where `n` is the 1-indexed iteration count and `N` is
`SELF_CRITIQUE_MAX_ITERATIONS + 1` (initial evaluation + retries).

**Acceptance criteria**:

- A unit test captures the logger output (via `caplog`) and asserts the
  format is matched exactly for the LLM path.
- A unit test asserts the fast-path emits a distinct prefix
  `[CRITIQUE][fast-path]` with the dropped filters listed.
- The format SHALL be machine-parseable (key=value pairs) so log
  aggregators can extract per-iteration distributions.

---

### Backwards Compatibility (REQ-CRITIQUE-COMPAT-*)

#### REQ-CRITIQUE-COMPAT-001 — User-driven `crit:more` / `crit:less` SHALL be unchanged [P0]

**THE SYSTEM SHALL** preserve the behavior of `app/graphs/nodes/critique_apply.py`
and the user-facing critique callbacks (`crit:more`, `crit:less`,
`crit:fewer`, brand-exclusion buttons) shipped in PR #10. Self-critique
is internal-only and SHALL NOT interact with the user's explicit critique
signals.

**Acceptance criteria**:

- The user-driven critique tests from SPEC-AGENT-001 (REQ-AGENT-007) SHALL
  pass unchanged.
- A user-issued `crit:less` callback SHALL reset
  `WorkingState.critique_retry_count` to `0` (the user is starting a new
  search round; self-critique starts fresh).
- A user-issued `crit:less` SHALL clear `critique_pending_delta`,
  `critique_trail`, and `critique_exhausted` so the new search round is
  not contaminated by a prior round's self-critique state.
- A unit test exercises a full self-critique round (2 retries,
  exhaustion) followed by a user `crit:less` and asserts the second
  search round's evaluator iteration starts at `critique_retry_count=0`
  with an empty `critique_trail`.
- The reset rule SHALL be implemented in `critique_apply` (the consumer
  of user-driven callbacks), not in `evaluator` — keeps responsibilities
  clean.

#### REQ-CRITIQUE-COMPAT-002 — All 162 existing tests SHALL pass [P0]

**THE SYSTEM SHALL** preserve the pass status of every existing test in
the `tests/` tree (162 at SPEC freeze date). Tests that exercise the
graph topology MAY require updates ONLY when they directly assert the
absence of an `evaluator` node — and even those updates SHALL be
parameterized over `SELF_CRITIQUE_ENABLED` so the original assertion
remains valid for the `False` case.

**Acceptance criteria**:

- After the migration, `pytest -q` runs to completion with at least the
  same number of passing tests as before, plus the new tests added by
  this SPEC.
- Any topology-asserting test that previously hard-coded
  `assert "evaluator" not in graph.nodes` SHALL be parameterized over
  the flag (`if settings.SELF_CRITIQUE_ENABLED: assert "evaluator" in
  graph.nodes else: assert "evaluator" not in graph.nodes`).
- The SPEC-AGENT-001 REQ-COMPAT-* terminal-flow tests (link-fail,
  vision-fail, vision-empty-result, multi-pick-sent-only, ask-clarify,
  search-empty, search-with-results, taste-only, off-topic) SHALL all
  continue to pass with `SELF_CRITIQUE_ENABLED=true`.
- `search-empty` flow SHALL behave equivalently — the user still gets a
  graceful empty-result reply; the only difference is that internally the
  loop tried one broaden-retry first.

#### REQ-CRITIQUE-COMPAT-003 — Disable flag SHALL produce byte-identical behavior [P0]

**WHEN** `SELF_CRITIQUE_ENABLED=false`,
**THE SYSTEM SHALL** behave identically to the pre-migration codebase:
the graph runs `search_node → send_results` with no detour, no state
field writes for `critique_*`, no Langfuse `evaluator` span, no log
lines with the `[CRITIQUE]` prefix.

**Acceptance criteria**:

- An integration test runs the same fixture conversation under both
  `SELF_CRITIQUE_ENABLED=true` and `SELF_CRITIQUE_ENABLED=false` and
  asserts the user-visible output is byte-identical when the score
  would have been ≥ 0.6 on iteration 0 (i.e., the only-iteration case).
- An integration test asserts that when `SELF_CRITIQUE_ENABLED=false`,
  zero Langfuse spans named `evaluator` are emitted across a full
  graph run.

---

### Out-of-Scope (REQ-CRITIQUE-OUT-OF-SCOPE-*)

#### REQ-CRITIQUE-OUT-OF-SCOPE-001 — Explicit non-goals [P0]

**THE SYSTEM SHALL NOT** implement any of the following as part of this
SPEC:

1. **Episodic memory (B4).** Cross-turn / cross-session learning from
   self-critique outcomes is deferred to a separate SPEC.
2. **Onboarding interview (B6).** No new user-facing question flow.
3. **Changes to `app/graphs/nodes/critique_apply.py`.** User-driven
   critique stays as-is.
4. **Changes to the search RPC `search_products_v5`.** No SQL changes,
   no scoring changes, no ranking changes.
5. **Changes to portal/app's web `/recommend` flow.** Web is single-shot;
   self-critique is Telegram-bot-only.
6. **A vision-based evaluator.** v1 evaluator runs on text metadata only.
7. **A typing-indicator UX touch** ("잠깐만요, 더 좋은 결과 찾는 중…")
   during retries. Tracked as Open Question 2 — likely deferred to a
   follow-up UX SPEC.
8. **Streaming evaluator output, tool-calling evaluator, or multi-turn
   evaluator dialogue.** Single-shot JSON extraction only.
9. **Per-tolerance dynamic score thresholds.** v1 uses a fixed threshold;
   per-tolerance tuning is tracked as Open Question 3.
10. **Persistent `critique_trail` history beyond the in-memory session.**
    The trail is per-graph-invocation only.

**Acceptance criteria**:

- Each item in the list above SHALL appear in the `Exclusions (What NOT
  to Build)` section verbatim.
- Each item that has a tracked Open Question (Q1–Q8) SHALL be linked
  back to the question.

---

## Environment Variables (introduced or modified by this SPEC)

| Var | Required | Default | Description |
|-----|----------|---------|-------------|
| `SELF_CRITIQUE_ENABLED` | no | `true` | Master flag for the self-critique loop. When `false`, falls back to the pre-SPEC behavior (`search_node → send_results` with no detour). REQ-CRITIQUE-COST-001. |
| `SELF_CRITIQUE_MAX_ITERATIONS` | no | `2` | Maximum number of retry iterations after the initial search. Total search invocations = 1 + this. REQ-CRITIQUE-RETRY-001. |
| `SELF_CRITIQUE_THRESHOLD` | no | `0.6` | Score threshold at which the evaluator decides "good enough" and exits the loop. REQ-CRITIQUE-RETRY-001. |
| `SELF_CRITIQUE_TIMEOUT_S` | no | `30.0` | Total wall-clock budget for the entire critique loop (initial + retries), in seconds. REQ-CRITIQUE-LOOP-SAFETY-003. |
| `SELF_CRITIQUE_FASTPATH_DROP_FILTERS` | no | `min_price,max_price,exclude_keywords` | Comma-separated list of filter names dropped on the empty-result fast-path. REQ-CRITIQUE-EMPTY-001. |
| `EVALUATOR_MODEL` | no | `gpt-4o-mini` | LLM model for the evaluator's score+reasoning call. REQ-CRITIQUE-EVAL-003. |
| `EVALUATOR_MAX_TOKENS` | no | `400` | Response cap for the evaluator's LLM call. REQ-CRITIQUE-EVAL-003. |
| `EVALUATOR_TEMPERATURE` | no | `0.2` | Sampling temperature for the evaluator's LLM call. REQ-CRITIQUE-EVAL-003. |
| `EVALUATOR_TIMEOUT_S` | no | `8.0` | Per-call timeout for the evaluator's LLM call. REQ-CRITIQUE-EVAL-003. |

All env vars are read once at startup via `app/core/config.py::Settings`
and exposed as typed properties. Hot-reload is not supported; flag flips
require a server restart (consistent with SPEC-MSG-001 and SPEC-AGENT-001).

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | **Cost regression**: every search now triggers an evaluator LLM call (~$0.0005), and full-retry paths add 2 more (~$0.0015 worst case). At 10× current Telegram volume the marginal monthly cost climbs noticeably. | High (by design) | Medium | The feature flag (`SELF_CRITIQUE_ENABLED`) gives one-flip rollback. The fast-path (REQ-CRITIQUE-EMPTY-001) skips the LLM on the most common failure mode (0 candidates). Per-iteration cost is tracked on the Langfuse span (REQ-CRITIQUE-COST-002) for weekly review. The 8s `EVALUATOR_TIMEOUT_S` bounds the worst-case cost per call. |
| R2 | **Latency regression**: adding an evaluator + up to 2 retries can push tail latency from ~7s (current p95) to ~15s+ on retry-heavy turns. | High (by design) | High | The total `SELF_CRITIQUE_TIMEOUT_S=30` cap is the hard ceiling. P50 overhead is expected to be ≤ 1s (single fast-path or single LLM eval). P95 with full retries is expected ≤ 8s. SPEC-MSG-001's 12s end-to-end target is a soft goal; on retry-heavy turns we knowingly cross it but stay below the absolute cap. The flag flips on regression. UX mitigation (typing indicator) is tracked as Open Question 2. |
| R3 | **Loop oscillation**: evaluator suggests delta A, search returns same poor results, evaluator suggests delta A again. | Medium | High | Stagnation guard (REQ-CRITIQUE-LOOP-SAFETY-002) exits on duplicate delta. Set-equivalence comparison covers reordering tricks. Independently, REQ-CRITIQUE-LOOP-SAFETY-002a exits on score regression. Independently, REQ-CRITIQUE-LOOP-SAFETY-001 hard-caps iterations. Three orthogonal guards. |
| R4 | **User confusion** from longer perceived wait time during silent retries. | Medium | Medium | v1 ships silent (Open Question 2 — typing-indicator deferred). The natural-language `respond` reply softens its tone when `critique_exhausted=true` (REQ-CRITIQUE-RETRY-003), so the user gets a coherent acknowledgment of the difficulty rather than just a delayed empty result. If user complaints surface in dev, the typing-indicator follow-up SPEC ships next cycle. |
| R5 | **Evaluator hallucinates suggestions that make results worse.** Score on iteration N+1 < score on iteration N. | Medium | High | REQ-CRITIQUE-LOOP-SAFETY-002a exits on score regression and ships the PREVIOUS iteration's higher-scoring set. The evaluator's prompt explicitly instructs it to suggest deltas that strictly improve match quality, but the runtime guard catches misbehavior even if the prompt drifts. |
| R6 | **Evaluator outage cascade**: LiteLLM degraded → evaluator timeout on every turn → every retry path silently fails → users see degraded but not failed bot. | Low | Medium | REQ-CRITIQUE-EVAL-003 fail-open: evaluator failure returns synthetic `score=1.0, retry=False` so the user gets the original results, not a worse retry-derived set. Fail-open is logged at WARNING so on-call sees the cascade in time to flip the flag. |
| R7 | **`critique_pending_delta` leak across graph invocations** if a state field is not properly cleared between `graph.ainvoke` calls. | Low | High | REQ-CRITIQUE-RETRY-001 mandates `search_node` clears the field after consumption. SPEC-AGENT-001's existing pattern of fresh `WorkingState` per `graph.ainvoke` covers cross-invocation isolation; this SPEC does not break that. A unit test asserts the field is `None` at the start of every `graph.ainvoke`. |
| R8 | **Retry budget exhaustion masks a real prompt-engineering bug**: every turn always exhausts → looks like "feature works" in metrics but actually evaluator never approves anything. | Medium | Medium | Langfuse metadata exposes `score` distribution per iteration (REQ-CRITIQUE-OBSV-001). Weekly review (REQ-CRITIQUE-COST-002) catches the pattern. If P50 score is < 0.6, prompt or threshold needs tuning. |
| R9 | **`CritiqueDelta` schema drift** between the evaluator's prompt-induced JSON output and the consumer (`search_node`) — extra fields in JSON cause `extra="forbid"` validation failure. | Medium | Medium | REQ-CRITIQUE-EVAL-003 fail-open: validation failure returns synthetic `retry=False` so the user gets the original results. The validation-failure log line surfaces the unknown field for prompt tuning. `plan.md` may opt for `extra="ignore"` if the operational signal turns into noise — decision recorded there. |
| R10 | **Stagnation-guard false positive**: the evaluator legitimately wants to retry the same delta because the search was non-deterministic on the first attempt (cache miss vs hit, etc.). | Low | Low | The search RPC is deterministic given the same query (no randomness in `search_products_v5`); retrying the same delta WILL produce the same candidates. Therefore stagnation is a true signal. If non-determinism is later introduced upstream, the guard's set-equivalence check can be relaxed in a follow-up. |
| R11 | **`OutputState.critique_exhausted` plumbing miss**: `respond` does not actually read the flag and the UX softening never lands. | Medium | Medium | REQ-CRITIQUE-RETRY-003 has an explicit acceptance test for the rendered-text difference. CI guards against regression. |
| R12 | **Total LLM token cost across the whole evaluator+respond+vision chain** crosses an internal budget threshold that nobody is tracking. | Low | Medium | Each LLM call has its own Langfuse span with token counts; the existing observability infrastructure (SPEC-AGENT-001 REQ-OBSV-*) sums them. The flag flips if cost exceeds the 3× baseline (REQ-CRITIQUE-COST-002). |
| R13 | **`SELF_CRITIQUE_MAX_ITERATIONS` set to a high value in production** (operational error) causes 6× search RPC load on the 0-candidate path. | Low | High | The env var is loaded at startup; an out-of-range guard in `app/core/config.py` clamps it to `[0, 5]` (with a startup log warning if a higher value was attempted). Specified in `plan.md`. |
| R14 | **Langfuse `evaluator` span volume doubles existing trace size** at peak Telegram throughput. | Low | Low | The new span adds ~0.5 KB per iteration (REQ-CRITIQUE-OBSV-001). At a 2-retry max, ~1.5 KB per turn. Langfuse self-host headroom (per the kiko.ai infra) covers this. |

---

## Exclusions (What NOT to Build)

The following are explicitly out of scope for SPEC-AGENTIC-CRITIQUE-001
and MUST NOT be implemented as part of this SPEC:

1. **Episodic memory (B4) — no cross-turn / cross-session learning** from
   self-critique outcomes. The `critique_trail` lives only inside a single
   `graph.ainvoke` call. Tracked as a separate roadmap item.
2. **Onboarding interview (B6) — no new user-facing question flow** based
   on prior self-critique results.
3. **Changes to user-driven `crit:more` / `crit:less` callbacks.**
   `app/graphs/nodes/critique_apply.py` and the carousel-button handlers
   from PR #10 are unchanged.
4. **Changes to the search pipeline RPC (`search_products_v5`)**, the
   embedding model, the diversity capper, or the `enhance_query` feature
   flag. Only the loop AROUND the pipeline is added.
5. **Changes to portal/app's web `/recommend` flow.** Web stays
   single-shot; self-critique is Telegram-bot-only. The shared
   `RecommendRequest` DTO is unchanged.
6. **Vision-based evaluator**. The v1 evaluator runs on text metadata
   only (product titles, brands, categories, tags). Image-based
   evaluation tracked as Open Question 1.
7. **Typing-indicator UX during retries** ("잠깐만요, 더 좋은 결과 찾는
   중…"). v1 ships silent. Tracked as Open Question 2.
8. **Streaming evaluator output, tool-calling evaluator, or multi-turn
   evaluator dialogue.** Single-shot JSON extraction only, same shape as
   `vision.extract`.
9. **Per-tolerance dynamic score thresholds.** v1 uses a fixed
   `SELF_CRITIQUE_THRESHOLD=0.6`. Per-tolerance tuning tracked as Open
   Question 3.
10. **Reusing the evaluator on user-driven `crit:more` callbacks.** When
    the user explicitly asks for "more like #N", the evaluator is
    bypassed — the user's signal trumps self-critique. Tracked as Open
    Question 4.
11. **Persistent `critique_trail` storage beyond the in-memory
    `WorkingState`.** No DB-backed retry history. Long-horizon analysis
    relies on Langfuse traces, not on persisted state.
12. **Removing the `SELF_CRITIQUE_ENABLED` flag.** Deferred to a
    follow-up SPEC once the flag has stabilized at `True` for at least
    one release.
13. **Removing or refactoring `app/channels/critique.py`** (the
    user-driven critique helper). Self-critique is internal-only and
    distinct.
14. **Changing the Vision call's `max_tokens` / `temperature` / prompt
    body.** SPEC-VISION-UNIFY-001 owns those; this SPEC consumes the
    rich Vision schema as read-only context.
15. **A side-by-side schema-diff dashboard for `CritiqueScore` /
    `CritiqueDelta`.** The Pydantic `extra="forbid"` validation +
    fail-open path is the only drift-detection mechanism.
16. **Group chats, channels, payments, Stars, Mini Apps.** Inherited
    1:1 DM scope from SPEC-MSG-001.
17. **A separate self-critique configuration UI / runtime override.** All
    tuning is via env vars.

---

## Open Questions (to resolve during plan.md / implementation)

These do not block SPEC approval but should be answered before code is
written:

1. **Vision-based vs text-only evaluation.** Should the evaluator have
   access to the actual product images (vision-based eval) or just text
   metadata (titles, brands, categories, tags)? v1 SHALL be text-only for
   cost reasons (~10× cost difference). Vision-based eval is a follow-up
   if quality plateaus. `plan.md` confirms.
2. **Silent retry vs typing-indicator UX.** Should the bot send a
   "잠깐만요, 더 좋은 결과 찾는 중…" `sendChatAction(typing)` or a
   transient text message during retries? v1 leans silent (just a longer
   wait); a UX follow-up SPEC adds the indicator if user complaints
   surface. `plan.md` confirms.
3. **Score threshold: fixed vs per-tolerance dynamic.** Start with fixed
   `SELF_CRITIQUE_THRESHOLD=0.6`, configurable via env. Per-tolerance
   tuning (e.g., 0.5 for casual browse, 0.7 for shopping intent) is
   tracked as a follow-up. `plan.md` confirms.
4. **`crit:more` interaction with the evaluator.** When the user taps
   "more like #N" (the existing user-driven callback), should the
   re-search go through the evaluator? Lean NO — user's explicit signal
   trumps self-critique; flowing through the evaluator risks the
   evaluator overriding the user's intent. `plan.md` confirms.
5. **Where does the evaluator's prompt live?** REQ-CRITIQUE-EVAL-003
   mandates `app/graphs/nodes/evaluator_prompt.py` with the prompt as
   Python string constants (mirrors the `vision_prompt.py` pattern from
   SPEC-VISION-UNIFY-001). `plan.md` decides whether the prompt is a
   single opaque blob or includes structured context (vision item summary,
   user intent, taste profile excerpt).
6. **Stagnation-guard equality semantics.** Exact match on the
   `CritiqueDelta` object, or set-equivalence on the keys we care about
   (`exclude_brands`, `exclude_keywords`, `boost_keywords`, `color_override`,
   `fit_override`, `drop_filters`, `intent`)? Lean set-equivalence —
   field order should not matter. `plan.md` confirms and the helper
   `deltas_equivalent` is unit-tested with at least 6 fixture pairs
   (REQ-CRITIQUE-LOOP-SAFETY-002).
7. **User-driven `crit:less` reset semantics.** Does a user `crit:less`
   reset `critique_retry_count` to `0`? REQ-CRITIQUE-COMPAT-001 says YES
   (the user is starting a new search round). `plan.md` confirms and
   identifies the exact reset site in `critique_apply.py`.
8. **Softer `respond` copy on `critique_exhausted=true`.** REQ-CRITIQUE-RETRY-003
   mandates that `respond` reads the flag and produces visibly-different
   copy. The exact copy ("찾기 어려운 스타일이네요 — 비슷한 것들 먼저
   보여드려요" or English equivalent) is decided in `plan.md`. Reply
   language stays English per SPEC-MSG-001 REQ-MSG-005; the softening
   is in tone, not language.

---

## Future Scope (post-MVP, separate SPEC)

- **Episodic memory (B4).** Persist self-critique outcomes across turns
  / sessions; let the bot learn which delta intents work for which user
  styles.
- **Onboarding interview (B6).** Use prior self-critique outcomes to
  surface a clarifying question proactively at session start ("looks like
  we struggle with denim fit for you — what fit do you usually wear?").
- **Vision-based evaluator.** v2 evaluator that takes the candidate
  product images alongside the user's vision item and judges visual
  similarity directly.
- **Typing-indicator UX during retries.** `sendChatAction(typing)` or a
  transient "찾는 중…" message; lands as a follow-up if user complaints
  surface.
- **Per-tolerance dynamic threshold.** Score threshold tuned per
  user-intent class (casual browse vs shopping intent vs style research).
- **Removing the `SELF_CRITIQUE_ENABLED` flag.** Lands once the flag has
  defaulted to `True` for at least one release with no rollbacks.
- **Removing the legacy `search_node → send_results` direct edge.**
  Currently kept (behind the flag) for the regression-test toggle. A
  follow-up SPEC removes it once the feature has stabilized.
- **Cross-channel parity with web.** If self-critique proves valuable,
  port it to portal/app's web `/recommend` flow. (Currently web is
  single-shot by design.)
- **Episodic feedback signal.** When a user dwells on / clicks one of
  the retry-derived candidates, record this as positive signal for the
  delta intent that produced it. Feeds B4.

---

## Cross-References

- **Builds on**:
  - SPEC-MSG-001 (Telegram channel transport — kept unchanged).
  - SPEC-AGENT-001 (LangGraph topology — extended with one new node and
    one new conditional retry edge; existing 10-node topology preserved
    otherwise).
  - SPEC-VISION-UNIFY-001 (rich Vision schema feeds the evaluator's
    reasoning context — the evaluator reads `WorkingState.vision_result`
    and `WorkingState.vision_selected_item` as read-only inputs).
  - SPEC-PIPELINE-001 (search pipeline being looped — RPC and scoring
    unchanged; only the loop around it is added).
- **Affected modules in portal/ai**:
  - `app/graphs/nodes/evaluator.py` (NEW),
    `app/graphs/nodes/evaluator_prompt.py` (NEW),
    `app/graphs/state.py` (state extension),
    `app/graphs/routing.py` (new edge function `after_evaluator`),
    `app/graphs/fashion_bot.py` (graph wiring + flag-gated topology),
    `app/graphs/nodes/search.py` (delta application contract),
    `app/graphs/nodes/respond.py` (softer reply on `critique_exhausted`),
    `app/graphs/nodes/critique_apply.py` (reset retry state on user
    `crit:less` — REQ-CRITIQUE-COMPAT-001).
  - `app/core/config.py` (new env vars).
  - `.env.example` (new env vars documented).
- **Tests**:
  - `tests/test_graph_nodes/test_evaluator.py` (NEW — unit tests for
    `CritiqueScore` validation, fail-open paths, fast-path,
    `deltas_equivalent` helper),
  - `tests/test_graph_nodes/test_search.py` (extended for delta
    application on retry),
  - `tests/test_graph_flows.py` (extended with full-loop scenarios:
    healthy single-shot, fast-path success, full LLM retry success,
    budget exhaustion, stagnation, score regression, timeout),
  - `tests/test_graph_safety.py` (NEW — concentrates the 4 safety guards),
  - `tests/test_recommendation_port.py` (extended for new delta-derived
    `RecommendRequest` fields if `plan.md` opts to thread fields through
    the port DTO rather than via state-only).
- **Project context**: `/Users/hansangho/desktop/portal/ai/CLAUDE.md`.
- **PR baseline**: SPEC-VISION-UNIFY-001 (just merged) provides the rich
  Vision schema that the evaluator consumes. SPEC-AGENT-001 (PR #11,
  commit `f0a7f03`) provides the 10-node LangGraph this SPEC extends.

---

## Definition of Done (P0)

- [ ] REQ-CRITIQUE-EVAL-001 through REQ-CRITIQUE-EVAL-003 implemented;
      `evaluator` node exists; `CritiqueScore` and `CritiqueDelta`
      Pydantic models declared with `extra="forbid"`; fail-open path
      returns `score=1.0, retry=False` on every failure mode (timeout,
      HTTP error, JSON parse, validation error).
- [ ] REQ-CRITIQUE-RETRY-001 through REQ-CRITIQUE-RETRY-003 implemented;
      `WorkingState` carries `critique_retry_count`, `critique_trail`,
      `critique_pending_delta`, `critique_exhausted`,
      `critique_started_at_ms`; `search_node._build_request` consumes
      and clears `critique_pending_delta`; `OutputState.critique_exhausted`
      reaches `respond`.
- [ ] REQ-CRITIQUE-EMPTY-001 implemented; 0-candidate first iteration
      triggers a NO-LLM broaden delta; second occurrence DOES go through
      the evaluator LLM.
- [ ] REQ-CRITIQUE-LOOP-SAFETY-001 through REQ-CRITIQUE-LOOP-SAFETY-003
      implemented; iteration cap, stagnation guard (set-equivalence on
      delta), score-regression guard, and 30s wall-clock timeout all
      independently exit the loop and ship the best-so-far results.
- [ ] REQ-CRITIQUE-COST-001 and REQ-CRITIQUE-COST-002 implemented;
      `SELF_CRITIQUE_ENABLED=false` produces byte-identical pre-SPEC
      behavior; per-iteration cost documented in this SPEC + plan.md.
- [ ] REQ-CRITIQUE-OBSV-001 and REQ-CRITIQUE-OBSV-002 implemented;
      Langfuse `evaluator` span carries the documented metadata keys;
      per-iteration `[CRITIQUE][n/N]` log line emitted in the documented
      format.
- [ ] REQ-CRITIQUE-COMPAT-001 through REQ-CRITIQUE-COMPAT-003
      implemented; user-driven `crit:more` / `crit:less` unchanged;
      `crit:less` resets retry state; all 162 existing tests pass under
      both flag values.
- [ ] `app/graphs/nodes/evaluator.py` carries the `@MX:ANCHOR` annotation
      at the entry point (fan_in: `search_node` + retry path), with
      explicit `@MX:NOTE` on the fail-open synthetic-score path.
- [ ] `app/core/config.py` and `.env.example` declare all 9 new env
      vars (`SELF_CRITIQUE_*`, `EVALUATOR_*`) with documented defaults.
- [ ] An end-to-end manual test against the dev Telegram bot exercises:
      (a) clear-intent photo (e.g., the "blue jeans" image from the
      observed incident) → evaluator approves on iteration 0 → user gets
      results in single-shot timing.
      (b) ambiguous-result photo → evaluator scores 0.4 → retry with a
      narrower delta → evaluator scores 0.7 → user gets the
      retry-derived result set.
      (c) zero-candidate photo (overly-narrow filter) → fast-path
      broaden retry returns candidates → user gets results without an
      LLM-evaluator call on iteration 0.
      (d) zero-candidate photo where even broaden returns 0 → evaluator
      LLM eval on iter 1 → eventual exhaustion → user gets empty result
      with softened `critique_exhausted=true` reply.
      (e) `SELF_CRITIQUE_ENABLED=false` restart → evaluator absent from
      compiled graph, no `[CRITIQUE]` log lines, single-shot behavior
      restored.
- [ ] Cost / latency observation: a one-week sample on the dev bot
      shows P50 added latency ≤ 1s, P95 added latency ≤ 8s, P95 retry
      depth ≤ 1 (most turns approve on iteration 0 or fast-path).
- [ ] `ruff check . && ruff format --check .` passes.
- [ ] `pytest -q` passes at the same or higher count vs pre-SPEC
      baseline.
