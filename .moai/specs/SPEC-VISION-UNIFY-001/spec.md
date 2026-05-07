---
id: SPEC-VISION-UNIFY-001
version: 0.1.0
status: draft
created: 2026-05-07
updated: 2026-05-07
author: hchsa77@gmail.com
priority: P0
issue_number: null
---

# SPEC-VISION-UNIFY-001: Unify Telegram Vision Schema with portal/app Web Vision

## HISTORY

- 2026-05-07 (v0.1.0): Initial draft. Captures the decision (Option A) to keep
  TWO Vision call sites — portal/app's `src/lib/prompts/analyze.ts` (web) and
  portal/ai's `app/channels/vision.py` (Telegram bot) — but force them to emit
  the SAME rich JSON schema so that downstream search quality is identical
  regardless of entry channel. Builds on SPEC-MSG-001 (Telegram channel
  transport — kept), SPEC-AGENT-001 (LangGraph topology — extended through
  Working/Session state plumbing), and SPEC-PIPELINE-001 (search pipeline —
  the consumer of the new rich query fields). Two alternatives were
  considered and explicitly rejected: Option B (have the Telegram bot call
  portal/app's `/api/analyze` over HTTP) and Option C (extract Vision into a
  shared microservice). Both deferred — see "Non-Goals" and "Future Scope".

---

## Goal

The kiko.ai stack currently runs TWO independent GPT-4o-mini Vision
implementations that have drifted apart:

1. **portal/app (Next.js web)** at
   `src/lib/prompts/analyze.ts` (149-line `ANALYZE_SYSTEM_PROMPT`) +
   `src/lib/analyze/run-vision.ts` — produces a RICH outfit-and-items schema
   used by the web `/recommend` flow (`isApparel` gate, `styleNode`,
   `sensitivityTags`, `mood`, `palette`, `style`, fully-enum-constrained
   `items[]` with `searchQuery` / `searchQueryKo`).
2. **portal/ai Telegram bot (this repo)** at `app/channels/vision.py` —
   produces a MINIMAL `items[] {label, description, color, keywords[]}` shape.
   The Telegram search node (`app/graphs/nodes/search.py::_build_request`)
   only forwards `keywords + intent` into `ChannelRecommendationRequest`,
   which then maps to `RecommendRequest` with `searchQueryKo=None` and no
   subcategory / fit / colorFamily / styleNode plumbing.

**Result**: identical input photos produce materially worse search results in
the Telegram channel than on the web, because the Telegram path bypasses the
sparse-query enrichment and enum-driven matching that `search_products_v5`
relies on.

This SPEC unifies the schema along **Option A**: portal/ai keeps owning its
own Vision call (no microservice extraction, no cross-process call to
portal/app), but adopts the SAME rich JSON schema, threads the new fields
through `WorkingState` / `SessionState` / `ChannelRecommendationRequest`, and
reaches the underlying `RecommendRequest` with `searchQueryKo`, `subcategory`,
`fit`, `colorFamily`, `styleNode`, `moodTags`, and `gender` populated.

The migration is **schema + plumbing only**: no new product capabilities, no
new Vision model, no new agentic features. The acceptance bar is parity with
portal/app's web search quality on the same content (REQ-VISION-PARITY-*).

This SPEC describes WHAT the new schema and plumbing must look like. Exact
prompt text reuse strategy, file structure, and rollback toggle wiring belong
in `plan.md`.

## Non-Goals

- **Replacing SPEC-MSG-001.** Channel transport (Telegram adapter, webhook
  parser, secret-token verification) is reused unchanged.
- **Replacing SPEC-AGENT-001.** The 10-node LangGraph topology is preserved.
  Only the Vision node's output shape, the picker's selection contract, and
  the search node's request builder are extended.
- **Replacing SPEC-PIPELINE-001.** The `embed → enhance_query → search →
  diversify` pipeline runs unchanged. This SPEC only enriches the inputs
  reaching `RecommendRequest`.
- **Migrating Vision to a shared microservice (Option C).** Vision continues
  to run in-process inside `portal/ai`. A future SPEC may extract it once
  the prompt has stabilized through at least two release cycles in both
  channels.
- **Calling portal/app's `/api/analyze` from the Telegram bot (Option B).**
  Avoids cross-deployment latency, an extra failure domain, and an auth
  surface between the bot and Next.js. The bot keeps its own Vision call.
- **Modifying portal/app's prompt or schema.** This SPEC is one-way:
  portal/ai converges to portal/app's schema. Any future schema change MUST
  start in portal/app and only land in portal/ai once REQ-VISION-PARITY-001
  can be re-verified.
- **Introducing new agentic features.** Self-critique loops, multi-image /
  outfit composition (H3), episodic memory, and tool-calling agents are out
  of scope and tracked separately.
- **Changing the Vision model itself.** GPT-4o-mini via LiteLLM remains the
  Vision backend. Only `max_tokens`, `temperature`, and the prompt body
  change.
- **Rewriting the four LLM modules from SPEC-AGENT-001 (REQ-LLM-001).**
  `router.py`, `critique.py`, `enhance_query.py`, and the LangChain-wrapped
  `respond` / `ask_clarify` nodes are unaffected.
- **Streaming Vision output, multi-turn Vision context, or Vision tool
  calls.** Single-shot JSON-extraction call, same as today.

## Stakeholders

| Role | Responsibility |
|------|----------------|
| Product / Founder (hchsa77@gmail.com) | Approves the parity acceptance bar (web Telegram quality should be indistinguishable on the same image). Sign-off on REQ-VISION-PARITY-* and on the rollback flag (REQ-VISION-COMPAT-005). |
| AI Server Owner (this SPEC) | All work in `app/channels/vision.py`, `app/graphs/state.py`, `app/channels/session.py`, `app/graphs/nodes/{vision,pick_item,search}.py`, `app/channels/recommendation.py`, `app/models/request.py`, plus the new `app/channels/vision_prompt.py` canonical prompt module. Owns parity tests and per-node tests. |
| portal/app Owner | Read-only contract source. Signs off that the schema captured in `vision_prompt.py` is verbatim against `analyze.ts` at the SPEC freeze date. After freeze, any divergence triggers a sync PR (RISK R3). |
| Langfuse Operator | Verifies the new metadata fields (`subcategory`, `fit`, `colorFamily`, `searchQueryKo`, `styleNode`, `moodTags`, `gender`) appear on the `vision_extract` span and that no PII leaks. |
| Modal Team | Out of scope — embeddings unchanged. |

---

## Architecture Snapshot (informative)

```
[Telegram webhook]                              [Next.js web /recommend]
        │                                                │
        ▼                                                ▼
graph.ainvoke (SPEC-AGENT-001)                src/lib/prompts/analyze.ts
        │                                       (ANALYZE_SYSTEM_PROMPT)
        ▼                                                │
app/channels/vision.py::extract                          ▼
        │                                       run-vision.ts (rich schema)
        │                                                │
        ▼                                                ▼
WAS: minimal {label,desc,color,kw[]}        ALREADY: rich {styleNode, mood,
                                              palette, style, items[…]}
        │                                                │
        └──────────── ONE SHARED SCHEMA ─────────────────┘  (REQ-VISION-UNIFY-001)
                              │
                              ▼
                   AI server pipeline (/recommend, search_products_v5)
                              │
                              ▼
                       SAME quality bar
```

**Affected modules in portal/ai (this SPEC)**:

- `app/channels/vision.py` — rewritten to emit rich schema, return Pydantic
  model, raise `max_tokens=2500` / `temperature=0.3` parity (REQ-VISION-002,
  REQ-VISION-003).
- `app/channels/vision_prompt.py` — NEW. Holds the verbatim copy of
  `ANALYZE_SYSTEM_PROMPT` plus auxiliary enum / taxonomy reference text
  (REQ-VISION-UNIFY-001).
- `app/graphs/state.py` — `WorkingState` extended with new
  `vision_outfit` (outfit-level fields) and the picker's
  `selected_item_index` already covers per-item resolution
  (REQ-VISION-STATE-001).
- `app/channels/session.py` — `SessionState` extended with the new
  outfit-level + selected-item fields persisted across webhooks
  (REQ-VISION-STATE-002).
- `app/graphs/nodes/vision.py` — node updated to read the new model;
  `_is_weak_vision` predicate in `app/graphs/routing.py` re-evaluated against
  the richer schema (REQ-VISION-WEAKVISION-001).
- `app/graphs/nodes/pick_item.py` — picker preserves full structured item
  context, not just `label + keywords` (REQ-VISION-STATE-003).
- `app/graphs/nodes/search.py::_build_request` and
  `app/channels/recommendation.py::ChannelRecommendationRequest` —
  request DTO carries the rich fields end-to-end into `RecommendRequest`
  (REQ-VISION-SEARCH-001, REQ-VISION-SEARCH-002).
- `app/observability/langfuse.py` integration — `vision_extract` span
  metadata expanded (REQ-VISION-OBSV-001).
- `tests/test_graph_nodes/test_vision.py`, `tests/test_graph_flows.py`,
  `tests/test_recommendation_port.py` — updated to assert the new schema
  and the end-to-end propagation (REQ-VISION-COMPAT-001).

**Reused, untouched modules**:

- `app/channels/telegram/*`, `app/channels/adapter.py`,
  `app/channels/factory.py`, `app/channels/critique.py`,
  `app/channels/router.py`, `app/channels/taste_profile.py`,
  `app/channels/link_resolver.py` — channel surface unchanged.
- `app/pipeline/runner.py`, `app/pipeline/embed.py`,
  `app/pipeline/enhance_query.py`, `app/pipeline/search.py`,
  `app/pipeline/diversify.py` — search pipeline unchanged.
- `app/providers/llm.py`, `app/providers/embedding.py`,
  `app/providers/database.py` — providers unchanged.
- `app/api/recommend.py`, `app/api/health.py`,
  `app/api/webhooks/telegram.py` — API surface unchanged.

---

## Schema Reference (informative — formalized in REQ-VISION-UNIFY-001)

The unified schema mirrors `portal/app/src/lib/prompts/analyze.ts`
verbatim. Outfit-level fields:

| Field | Type | Notes |
|-------|------|-------|
| `isApparel` | `bool` | Gate. When `false`, items SHALL be empty list and downstream nodes SHALL route to `respond` with an off-topic reply. |
| `styleNode` | object | `{primary: str, primaryConfidence: float, secondary: str, secondaryConfidence: float, reasoning: str}`. Primary is a node ID from `STYLE_NODE_IDS`. portal/ai consumes this as opaque strings; only portal/app owns the taxonomy definition. |
| `sensitivityTags` | `list[str]` | 1-3 from `SENSITIVITY_TAGS` allowed list. Stored in Korean (matches portal/app). |
| `mood` | object | `{tags: [{label, score}], summary, vibe, season, occasion}`. |
| `palette` | `list[{hex, label}]` | 3-5 dominant colors. |
| `style` | object | `{fit, aesthetic, detectedGender}`. `detectedGender ∈ {male, female, unisex}`. |
| `items` | `list[Item]` | Per-item structured details. See below. |

Per-item fields:

| Field | Type | Notes |
|-------|------|-------|
| `id` | `str` | Unique within an outfit (e.g. `top`, `top_1`, `outer`). |
| `category` | `str` | Top-level category (`Top`, `Bottom`, `Outer`, `Shoes`, `Bag`, `Accessory`). |
| `subcategory` | `str` | Enum value (e.g. `t-shirt`, `overcoat`, `denim-pants`). |
| `name` | `str` | Short editorial name. |
| `detail` | `str` | Silhouette / construction detail. |
| `fabric` | `str` | Enum (e.g. `wool`, `jersey`, `denim`). |
| `color` | `str` | Specific color phrase. |
| `colorHex` | `str` | Hex code for dominant color. |
| `colorFamily` | `str` | Enum, UPPERCASE (e.g. `BLACK`, `GREY`). |
| `fit` | `str` | Enum: `oversized`, `relaxed`, `regular`, `slim`, `skinny`, `boxy`, `cropped`, `longline`. |
| `searchQuery` | `str` | English sparse query (`"[fit] [color] [fabric] [subcategory] [men/women]"`). |
| `searchQueryKo` | `str` | Korean sparse query, fashion industry vocabulary. |
| `position` | object | `{top: float, left: float}`, percent coordinates for UI dot placement. portal/ai bot does NOT render this dot, but the field SHALL still be persisted for parity tests and future UI parity. |

The Pydantic model in `app/channels/vision.py` SHALL forbid extra fields
(`ConfigDict(extra="forbid")`) so that drift between portal/app and portal/ai
fails fast.

---

## Requirements (EARS)

### Schema Unification (REQ-VISION-UNIFY-*)

#### REQ-VISION-UNIFY-001 — Telegram Vision SHALL emit the same JSON schema as portal/app's ANALYZE_SYSTEM_PROMPT [P0]

**WHEN** the Telegram bot extracts vision from an image,
**THE SYSTEM SHALL** issue a Vision (LiteLLM `gpt-4o-mini`) call whose
system prompt is byte-for-byte identical to
`portal/app/src/lib/prompts/analyze.ts::ANALYZE_SYSTEM_PROMPT` at the SPEC
freeze date, and whose response satisfies the schema documented in the
"Schema Reference" section above.

**Acceptance criteria**:

- A new module `app/channels/vision_prompt.py` exposes
  `ANALYZE_SYSTEM_PROMPT: str` and `ANALYZE_USER_PROMPT: str` as Python
  string constants, populated from a verbatim copy of
  `portal/app/src/lib/prompts/analyze.ts`. Auxiliary content that
  `analyze.ts` builds at runtime (the output of `buildNodeReference()`,
  `buildTagList()`, `STYLE_NODE_IDS`, `SENSITIVITY_TAGS`, and
  `buildEnumReference()`) SHALL be captured as static strings in the same
  module so the bot can run without depending on portal/app.
- The module's docstring documents the source path
  (`portal/app/src/lib/prompts/analyze.ts`) and the freeze date.
- A CI check (or a `noqa`-tagged TODO with a tracking issue) flags the
  module if the source-of-truth copy in `portal/app` diverges. The exact
  mechanism (manual diff in PR review, weekly bash diff, or a sync script)
  is left to `plan.md`; this SPEC requires only that drift cannot ship
  silently.
- A unit test reads the prompt constants and asserts they contain the
  marker substrings `"isApparel"`, `"styleNode"`, `"sensitivityTags"`,
  `"searchQueryKo"`, and `"colorFamily"` (smoke test against accidental
  truncation).
- The Vision call passes the prompt as the system message and the existing
  user prompt (`"Analyze this outfit photo. Identify all visible
  clothing items and the overall style mood."`) as the user message, plus
  the image content (URL or base64) — same multimodal message shape as
  today.

#### REQ-VISION-UNIFY-002 — `vision.extract()` SHALL return a typed Pydantic model with safe fallback [P0]

**THE SYSTEM SHALL** change the return type of `app.channels.vision.extract`
from `dict` to a Pydantic v2 model `VisionResult` defined in
`app.channels.vision` (or a sibling `app.channels.vision_models` module).
On any failure path (LiteLLM timeout, HTTP error, JSON parse failure,
Pydantic validation failure), the function SHALL return a fallback
`VisionResult` that satisfies the schema with empty / placeholder values
and SHALL NOT raise.

**Acceptance criteria**:

- `VisionResult` and the nested `VisionItem`, `VisionStyleNode`,
  `VisionMood`, `VisionPaletteEntry`, `VisionStyle`, `VisionMoodTag`,
  `VisionPosition` models are Pydantic v2 `BaseModel` classes with
  `model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)`.
- A successful call returns a `VisionResult` with `isApparel=True` (when
  the image contains apparel) and at least one `VisionItem`.
- A non-apparel image returns a `VisionResult` with `isApparel=False` and
  `items=[]` per the prompt's rule.
- The fallback returned on failure has `isApparel=False`, `items=[]`,
  `styleNode.primary="C"` (or the documented placeholder), empty `mood`,
  empty `palette`, and `style.detectedGender="unisex"`.
- The fallback path is exercised by a unit test that injects
  `httpx.TimeoutException`, an HTTP 5xx mock, and a malformed-JSON
  response, asserting `VisionResult` is returned in all three cases and
  the function does not raise.
- The `VisionResult` model is JSON-serializable so Langfuse metadata can
  embed it (REQ-VISION-OBSV-001).

#### REQ-VISION-UNIFY-003 — Vision call SHALL use parity max_tokens (2500) and temperature (0.3) [P0]

**THE SYSTEM SHALL** raise `max_tokens` from `600` to `2500` and
`temperature` from `0.2` to `0.3` for the Vision call in
`app.channels.vision.extract`, matching `run-vision.ts`'s LiteLLM payload.

**Acceptance criteria**:

- The constants in `app/channels/vision.py` (`_MAX_TOKENS=2500`,
  `_TEMPERATURE=0.3`) match the parameters portal/app sends.
- The values are exposed via `app/core/config.py` as
  `VISION_MAX_TOKENS` (default `2500`) and `VISION_TEMPERATURE`
  (default `0.3`) so future tuning does not require code change.
- `_VISION_TIMEOUT` SHALL be raised from `15.0` to a value sufficient for
  the larger response (e.g., `30.0`); exact value documented in
  `plan.md` based on observed P95 of `run-vision.ts`.
- A unit test patches `LLMProvider.chat` and asserts it receives
  `max_tokens=settings.VISION_MAX_TOKENS` and
  `temperature=settings.VISION_TEMPERATURE`.

---

### State Plumbing (REQ-VISION-STATE-*)

#### REQ-VISION-STATE-001 — `WorkingState` SHALL persist outfit-level and per-selected-item rich fields [P0]

**THE SYSTEM SHALL** extend `app/graphs/state.py::WorkingState` to carry
the new fields needed downstream. The minimum field set:

| Field | Type | Source | Consumer |
|-------|------|--------|----------|
| `vision_result` | `VisionResult \| None` | `vision_node` | All downstream nodes that need rich context |
| `vision_outfit_style_node_primary` | `str \| None` | `vision_result.styleNode.primary` | `search_node` (passed to RecommendRequest), `respond` (context) |
| `vision_outfit_style_node_secondary` | `str \| None` | `vision_result.styleNode.secondary` | `search_node`, `respond` |
| `vision_outfit_mood_tags` | `list[str]` | top labels from `vision_result.mood.tags` | `search_node`, `respond` |
| `vision_outfit_gender` | `str \| None` | `vision_result.style.detectedGender` | `search_node`, `respond` |
| `vision_selected_item` | `VisionItem \| None` | item at `selected_item_index`, or single item when only one detected | `search_node`, `pick_item` |

**Acceptance criteria**:

- The fields are declared with Pydantic v2 type hints and default values
  consistent with the rest of `WorkingState` (REQ-STATE-002 from
  SPEC-AGENT-001).
- A unit test instantiates `WorkingState` with only `InputState` fields
  and asserts the new fields take their declared defaults.
- A unit test updates `vision_result` via a state delta and asserts the
  derived convenience fields can be read directly (no implicit derivation
  inside Pydantic — nodes are responsible for projecting `vision_result`
  into the convenience fields).
- The `messages` reducer and the `log_events` reducer behavior from
  SPEC-AGENT-001 REQ-STATE-002 SHALL be preserved; this SPEC only adds
  fields.

#### REQ-VISION-STATE-002 — `SessionState` SHALL persist the rich vision context across webhooks [P0]

**THE SYSTEM SHALL** extend `app/channels/session.py::SessionState` so
that the rich vision context survives across webhooks within a
conversation. The minimum field set:

| Field | Type | Replaces / extends |
|-------|------|--------------------|
| `vision_result` | `VisionResult \| None` | extends — sits beside `vision_item` / `vision_keywords` for one release |
| `vision_selected_item_index` | `int \| None` | already exists implicitly via `selected_item_index`; canonicalize the name |
| `vision_outfit_style_node_primary` | `str \| None` | new |
| `vision_outfit_style_node_secondary` | `str \| None` | new |
| `vision_outfit_mood_tags` | `list[str]` | new |
| `vision_outfit_gender` | `str \| None` | new |

**Acceptance criteria**:

- The fields are added without removing the existing `vision_item` and
  `vision_keywords` fields, so backward compatibility (REQ-VISION-COMPAT-001)
  is preserved during the transition.
- The session store's serialization (in-memory today) round-trips the new
  fields without truncation.
- A two-webhook integration test (image upload turn 1 → critique callback
  turn 2) asserts that `search_node` in turn 2 has access to the rich
  fields populated in turn 1 (the critique-driven re-search uses the same
  `searchQueryKo` / `subcategory` / `fit` / `colorFamily` as the original
  search).

#### REQ-VISION-STATE-003 — `pick_item` SHALL preserve full structured item context [P0]

**WHEN** the user taps an item in the picker carousel,
**THE SYSTEM SHALL** record the full structured `VisionItem` for the
selected index in `WorkingState.vision_selected_item` and in
`SessionState.vision_result` / `SessionState.vision_selected_item_index`.
The current behavior of recording only `label + keywords` SHALL NOT
remain on the success path.

**Acceptance criteria**:

- A unit test for `app/graphs/nodes/pick_item.py` with a multi-item
  vision result and an `item:1` callback asserts that
  `vision_selected_item.subcategory`, `vision_selected_item.fit`,
  `vision_selected_item.colorFamily`, `vision_selected_item.searchQuery`,
  and `vision_selected_item.searchQueryKo` are all populated when
  `search_node` reads them.
- The legacy `vision_item` / `vision_keywords` fields SHALL still be set
  (derived from `vision_selected_item.name` and a flat tokenization of
  `vision_selected_item.searchQuery`) so SPEC-AGENT-001 REQ-COMPAT-* tests
  continue to pass without modification.
- The single-item-clear flow (only one detected item, `pick_item`
  skipped) SHALL also populate `vision_selected_item` with that single
  item — `search_node` MUST never see a `None` `vision_selected_item`
  when `vision_result.items` is non-empty.

---

### Search Request Enrichment (REQ-VISION-SEARCH-*)

#### REQ-VISION-SEARCH-001 — `ChannelRecommendationRequest` SHALL carry the rich item and outfit context [P0]

**THE SYSTEM SHALL** extend
`app/channels/recommendation.py::ChannelRecommendationRequest` to carry
the rich per-item and outfit context. New fields (additive — preserves
existing dataclass shape and existing call sites that omit them):

| Field | Type | Default | Source |
|-------|------|---------|--------|
| `item_subcategory` | `str \| None` | `None` | `vision_selected_item.subcategory` |
| `item_fit` | `str \| None` | `None` | `vision_selected_item.fit` |
| `item_fabric` | `str \| None` | `None` | `vision_selected_item.fabric` |
| `item_color_family` | `str \| None` | `None` | `vision_selected_item.colorFamily` |
| `item_search_query_en` | `str \| None` | `None` | `vision_selected_item.searchQuery` |
| `item_search_query_ko` | `str \| None` | `None` | `vision_selected_item.searchQueryKo` |
| `outfit_style_node_primary` | `str \| None` | `None` | `WorkingState.vision_outfit_style_node_primary` |
| `outfit_style_node_secondary` | `str \| None` | `None` | `WorkingState.vision_outfit_style_node_secondary` |
| `outfit_mood_tags` | `list[str]` | `[]` | `WorkingState.vision_outfit_mood_tags` |
| `outfit_gender` | `str \| None` | `None` | `WorkingState.vision_outfit_gender` |

**Acceptance criteria**:

- The dataclass fields are added with safe defaults so any call site that
  does NOT populate them continues to work (REQ-VISION-COMPAT-001).
- A unit test constructs a `ChannelRecommendationRequest` with only the
  pre-existing fields and asserts construction succeeds.
- A unit test constructs a `ChannelRecommendationRequest` with all new
  fields populated and asserts `frozen=True` semantics still hold.
- The `_build_query` helper SHALL be unchanged; the new fields participate
  in `RecommendRequest` mapping (REQ-VISION-SEARCH-002), not in the sparse
  query string composition.

#### REQ-VISION-SEARCH-002 — `PipelineRecommendationPort.recommend` SHALL map the rich fields into `RecommendRequest` [P0]

**WHEN** `PipelineRecommendationPort.recommend` builds `AnalyzedItem` and
`RecommendRequest`, **THE SYSTEM SHALL** populate the following fields
from the new `ChannelRecommendationRequest` fields when they are present:

| `RecommendRequest` / `AnalyzedItem` field | Source field on `ChannelRecommendationRequest` |
|-------------------------------------------|-------------------------------------------------|
| `AnalyzedItem.subcategory` | `req.item_subcategory` |
| `AnalyzedItem.color_family` | `req.item_color_family` (overrides today's `req.color`) |
| `AnalyzedItem.search_query` | `req.item_search_query_en` (when present) else fallback to today's composed `query` |
| `AnalyzedItem.search_query_ko` | `req.item_search_query_ko` |
| Additional fit / fabric / outfit context | Documented in `plan.md`; either passed via existing `RecommendRequest` extension fields or surfaced in metadata for `enhance_query` (SPEC-PIPELINE-001) |

**Acceptance criteria**:

- A unit test exercising `PipelineRecommendationPort.recommend` with a
  fully-populated `ChannelRecommendationRequest` asserts that
  `RecommendRequest.item.search_query_ko` matches
  `req.item_search_query_ko` byte-for-byte.
- A unit test with the legacy minimal `ChannelRecommendationRequest`
  (only `image_url`, `item_label`, `intent`, `keywords`) asserts behavior
  is identical to today (no regression).
- The mapping documents a STRICT precedence: rich fields win over legacy
  fields when both are present (e.g., `item_search_query_en` overrides
  the keyword-composed query). Precedence is asserted by tests.
- Whether `RecommendRequest` itself needs new optional fields
  (`fit`, `fabric`, outfit context) or whether those are already covered
  by `AnalyzedItem` is decided in `plan.md`. This SPEC requires only that
  the underlying `/recommend` path receives the rich context — exact
  field-level layout is implementation detail.

#### REQ-VISION-SEARCH-003 — `search_node._build_request` SHALL populate the rich fields when `vision_selected_item` is set [P0]

**WHEN** `search_node` builds the `ChannelRecommendationRequest`,
**THE SYSTEM SHALL** populate every new field from REQ-VISION-SEARCH-001
using the projection rules in REQ-VISION-STATE-001 and
REQ-VISION-STATE-003. The legacy keyword-only path SHALL be preserved as
a fallback when `vision_selected_item is None` (e.g., callback-driven
critique on a session where the user never uploaded an image — should be
impossible per the topology, but defended).

**Acceptance criteria**:

- A unit test with a `WorkingState` carrying a populated
  `vision_selected_item` asserts the produced
  `ChannelRecommendationRequest` carries the matching values for all 10
  new fields.
- A unit test with a `WorkingState` whose `vision_selected_item is None`
  asserts the produced request has all new fields at their defaults
  (no `AttributeError`, no exception).
- An integration test through `graph.ainvoke(...)` with a real-shape
  Vision response asserts that `RecommendRequest.item.search_query_ko`
  reaches `run_pipeline` (mocked) with the value
  `vision_result.items[selected].searchQueryKo`. This is the
  end-to-end parity check.

---

### Weak-Vision Re-evaluation (REQ-VISION-WEAKVISION-*)

#### REQ-VISION-WEAKVISION-001 — `_is_weak_vision` SHALL be re-evaluated against the rich schema [P0]

**THE SYSTEM SHALL** rewrite the `_is_weak_vision` predicate in
`app/graphs/routing.py` to use the rich schema. The new predicate SHALL
return true when ANY of the following hold for the SELECTED item (or the
single item if only one is detected):

1. `vision_result.isApparel == False` — non-apparel image; route to
   `respond` (off-topic), NOT to `ask_clarify`.
2. `vision_selected_item.subcategory` is empty OR matches the configured
   ambiguous-subcategory denylist (e.g., `item`, `clothing`, `piece`,
   defined via `ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES` env var).
3. `vision_selected_item.fit` is empty OR not in the documented enum
   (`oversized`, `relaxed`, `regular`, `slim`, `skinny`, `boxy`,
   `cropped`, `longline`).
4. `vision_selected_item.colorFamily` is empty.
5. `vision_selected_item.searchQuery` length is below the configured
   token threshold `ASK_CLARIFY_MIN_QUERY_TOKENS` (default `4`).

When the predicate fires AND the photo is apparel (rule 1 is false),
route to `ask_clarify`. When rule 1 fires (non-apparel), route directly
to `respond`.

**Acceptance criteria**:

- The existing `ASK_CLARIFY_MIN_DESC_TOKENS` and
  `ASK_CLARIFY_AMBIGUOUS_LABELS` env vars from SPEC-AGENT-001
  REQ-AGENT-009 SHALL remain documented and read but they map onto the
  new rules: `_MIN_DESC_TOKENS` is renamed to `_MIN_QUERY_TOKENS` (rule
  5) and `_AMBIGUOUS_LABELS` extends to
  `_AMBIGUOUS_SUBCATEGORIES` (rule 2). `plan.md` documents the rename
  and the deprecation timeline for the old var names.
- Unit tests cover all 5 rules independently against synthetic
  `VisionResult` instances.
- A unit test with a fully-populated rich-schema item asserts the
  predicate returns false (no false positive on healthy data).
- A unit test asserts that a non-apparel image (`isApparel=False`)
  routes to `respond`, NOT to `ask_clarify` (REQ-AGENT-006 invariant
  preserved).

---

### Backwards Compatibility (REQ-VISION-COMPAT-*)

#### REQ-VISION-COMPAT-001 — Existing tests SHALL pass with at most schema-level updates [P0]

**THE SYSTEM SHALL** preserve the behavior of every test that does not
directly assert the OLD minimal Vision schema. Tests that DO assert the
old minimal schema MAY be rewritten to assert the new rich schema, but
the underlying graph flow, session state propagation, and search results
SHALL remain equivalent.

**Acceptance criteria**:

- After the migration, `pytest -q` runs to completion with the same
  number of passing tests as before (modulo the new tests added by this
  SPEC).
- Any test that previously asserted
  `result["items"][0]["label"] == "..."` SHALL be rewritten as either
  `result.items[0].name == "..."` or
  `result.items[0].subcategory == "..."`, whichever maps closer to the
  intent of the original assertion.
- A separate "compat shim" test exercises a handful of downstream call
  sites that read the legacy `vision_item` and `vision_keywords` session
  fields and asserts those fields are still populated (derived from the
  rich result) so any external code path that has not been migrated yet
  continues to work.
- The SPEC-AGENT-001 REQ-COMPAT-* test matrix (9 terminal flows: link-fail,
  vision-fail, vision-empty-result, multi-pick-sent-only, ask-clarify,
  search-empty, search-with-results, taste-only, off-topic) SHALL all
  continue to pass.

#### REQ-VISION-COMPAT-002 — Telegram E2E test (image → result card) SHALL pass without regression [P0]

**WHEN** a fixture image is fed through the Telegram webhook,
**THE SYSTEM SHALL** dispatch a result card via the channel adapter
within the SPEC-MSG-001 12-second end-to-end budget.

**Acceptance criteria**:

- The existing E2E integration test (`tests/test_graph_flows.py` photo
  → results scenario) is updated to assert against the new
  `vision_result` shape but continues to verify exactly one outbound
  card-set + one `respond` text reply.
- P95 latency of the Vision call SHALL NOT exceed the
  `_VISION_TIMEOUT` documented in REQ-VISION-UNIFY-003. If the rich
  schema causes a P95 regression beyond the timeout, the rollback flag
  (REQ-VISION-COMPAT-005) SHALL be flipped pending a tuning round.

#### REQ-VISION-COMPAT-003 — Legacy session fields (`vision_item`, `vision_keywords`) SHALL remain populated for one release [P0]

**THE SYSTEM SHALL** continue to write the legacy `vision_item` and
`vision_keywords` fields on `SessionState` for one release after this
SPEC ships. These fields SHALL be derived from `vision_selected_item`
(`vision_item = vision_selected_item.name`,
`vision_keywords = a tokenization of vision_selected_item.searchQuery`)
when present, or take their pre-migration defaults otherwise.

**Acceptance criteria**:

- A unit test asserts that after a successful Vision extraction
  followed by a picker selection, both
  `session.vision_item == vision_selected_item.name` and
  `session.vision_keywords` is non-empty and shares tokens with
  `vision_selected_item.searchQuery`.
- A deprecation note is added to `app/channels/session.py` documenting
  the planned removal in a future SPEC.
- `plan.md` lists every read site of `vision_item` /
  `vision_keywords` and confirms each one has a migration path to the
  rich fields scheduled for the deprecation release.

#### REQ-VISION-COMPAT-004 — Fallback shape SHALL satisfy the rich schema [P0]

**IF** `vision.extract` fails (timeout, HTTP error, parse error,
validation error), **THEN** the returned `VisionResult` SHALL satisfy
the rich schema with the documented fallback values (REQ-VISION-UNIFY-002),
and downstream nodes SHALL handle this fallback equivalently to the
pre-migration behavior (route to `respond` with the existing
"sorry, I couldn't read that photo" message family).

**Acceptance criteria**:

- A unit test injecting a Vision timeout exercises the full graph and
  asserts the user sees the same kind of polite reply as pre-migration
  (matching SPEC-MSG-001 REQ-MSG-007).
- The fallback `VisionResult.isApparel == False` triggers the
  `_is_weak_vision` rule 1 (REQ-VISION-WEAKVISION-001) → `respond`
  branch — NOT `ask_clarify` (avoids spurious clarifying questions on
  pure infrastructure failures).

#### REQ-VISION-COMPAT-005 — `VISION_SCHEMA_V2` feature flag enables rollback [P0]

**THE SYSTEM SHALL** introduce a `VISION_SCHEMA_V2` env var (default
`true`). When set to `false`, `app.channels.vision.extract` SHALL fall
back to the pre-migration behavior: the old minimal prompt, the old
`{items: [{label, description, color, keywords}]}` dict shape (wrapped
in a thin `VisionResult` adapter so the return type stays stable), and
the legacy session fields take their pre-migration values.

**Acceptance criteria**:

- The flag lives in `app/core/config.py` (`Settings.VISION_SCHEMA_V2:
  bool = True`) and in `.env.example`.
- A unit test parameterized over the flag asserts:
  - `True` (default): rich schema, `searchQueryKo` reaches
    `RecommendRequest`.
  - `False`: minimal schema, `searchQueryKo is None` in
    `RecommendRequest`, no behavioral regression vs pre-SPEC state.
- The legacy code path remains in the codebase (e.g., behind a
  conditional in `extract()`) for one release. A follow-up SPEC removes
  it once the flag has been left at `True` in production for the
  documented stabilization window.
- The flag is logged at startup in `app/main.py` lifespan so on-call
  can verify it from the boot log.

---

### Observability (REQ-VISION-OBSV-*)

#### REQ-VISION-OBSV-001 — `vision_extract` Langfuse span SHALL include the new fields in metadata [P0]

**WHEN** `vision.extract` runs (whether under the SPEC-AGENT-001
`@observe`-instrumented graph entrypoint or the standalone `@observe`
decorator on `extract` itself), **THE SYSTEM SHALL** include the
following metadata on the `vision_extract` span:

| Metadata key | Value source |
|--------------|--------------|
| `is_apparel` | `vision_result.isApparel` |
| `style_node_primary` | `vision_result.styleNode.primary` |
| `style_node_secondary` | `vision_result.styleNode.secondary` |
| `mood_tags` | top labels from `vision_result.mood.tags` (max 5) |
| `detected_gender` | `vision_result.style.detectedGender` |
| `item_count` | `len(vision_result.items)` |
| `selected_item_subcategory` | `vision_selected_item.subcategory` (when picker has resolved) |
| `selected_item_fit` | `vision_selected_item.fit` |
| `selected_item_color_family` | `vision_selected_item.colorFamily` |
| `selected_item_search_query_ko` | `vision_selected_item.searchQueryKo` |
| `vision_schema_v2` | value of the `VISION_SCHEMA_V2` flag for the call |

**Acceptance criteria**:

- A unit test against a Langfuse mock asserts the metadata dict on the
  `vision_extract` span contains all 11 keys with values derived from a
  fixture `VisionResult`.
- No PII (raw `chat_id`, raw `from_user_id`) appears in any new field
  (SPEC-AGENT-001 REQ-OBSV-005 invariant preserved).
- When `VISION_SCHEMA_V2=False`, the span metadata SHALL only include the
  pre-migration keys plus `vision_schema_v2=false`; the rich keys SHALL
  be omitted (avoids ambiguous "null means failure vs null means flag-off"
  signals in dashboards).

#### REQ-VISION-OBSV-002 — Per-item logger output SHALL show subcategory + fit + colorFamily + searchQueryKo [P0]

**WHEN** `vision.extract` completes successfully,
**THE SYSTEM SHALL** log one structured INFO line per item containing
`subcategory`, `fit`, `colorFamily`, and a truncated `searchQueryKo`
preview (in addition to the existing index emoji and elapsed_ms
summary).

**Acceptance criteria**:

- The current per-item log line of the form
  `👁️  [VISION]   1️⃣ {label} — {description} [kw: {kw_preview}]`
  SHALL be replaced with
  `👁️  [VISION]   1️⃣ {subcategory}/{fit}/{colorFamily} — {searchQueryKo[:80]}`.
- A unit test captures the logger output (via `caplog`) and asserts the
  new format.
- The `elapsed_ms` and `items=N` summary line SHALL be preserved.

---

## Environment Variables (introduced or modified by this SPEC)

| Var | Required | Default | Description |
|-----|----------|---------|-------------|
| `VISION_SCHEMA_V2` | no | `true` | Master flag for the rich-schema behavior. When `false`, falls back to the pre-migration minimal schema (REQ-VISION-COMPAT-005). |
| `VISION_MAX_TOKENS` | no | `2500` | Vision LLM response cap, raised from `600` (REQ-VISION-UNIFY-003). |
| `VISION_TEMPERATURE` | no | `0.3` | Vision LLM sampling temperature, raised from `0.2` (REQ-VISION-UNIFY-003). |
| `VISION_TIMEOUT_S` | no | `30` | Per-call timeout in seconds, raised from `15` to fit the larger response (REQ-VISION-UNIFY-003). |
| `ASK_CLARIFY_MIN_QUERY_TOKENS` | no | `4` | Minimum token count in `vision_selected_item.searchQuery` below which `ask_clarify` fires (REQ-VISION-WEAKVISION-001). Renames `ASK_CLARIFY_MIN_DESC_TOKENS`. |
| `ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES` | no | `item,clothing,thing,piece` | Comma-separated denylist of ambiguous subcategory values that trigger `ask_clarify` (REQ-VISION-WEAKVISION-001). Extends `ASK_CLARIFY_AMBIGUOUS_LABELS`. |

The legacy `ASK_CLARIFY_MIN_DESC_TOKENS` and `ASK_CLARIFY_AMBIGUOUS_LABELS`
env vars from SPEC-AGENT-001 REQ-AGENT-009 SHALL still be read for one
release; their values map onto the new vars when the new vars are unset.
The deprecation removal lands in a follow-up SPEC.

---

## Risks & Mitigations

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| R1 | **Schema drift between portal/app and portal/ai** post-merge: portal/app updates `analyze.ts` but `vision_prompt.py` is not synced. | High | High | A CI / weekly diff check (mechanism chosen in `plan.md`) flags drift. The `vision_prompt.py` docstring carries the source-path reference and freeze date so divergence is visible in code review (REQ-VISION-UNIFY-001). |
| R2 | **Token cost regression**: `max_tokens=2500` × Telegram volume increases LLM spend. | High (by design) | Medium | The spend is bounded by the dev-only environment and the same per-call cap that portal/app already accepts. Langfuse + LiteLLM dashboards track per-turn cost (SPEC-AGENT-001 REQ-OBSV-004). The `VISION_SCHEMA_V2=false` flag provides a one-flip rollback path. |
| R3 | **Latency regression**: bigger response → slower Vision turn. | High (by design) | Medium | `VISION_TIMEOUT_S=30` cap. P95 monitored via Langfuse. If P95 exceeds the SPEC-MSG-001 12-second end-to-end budget, the flag flips (REQ-VISION-COMPAT-005). `plan.md` documents the SLO and the canary procedure. |
| R4 | **`extra="forbid"` Pydantic validation rejects responses** when GPT adds an undocumented field. | Medium | Medium | The fallback returned on validation failure satisfies the schema (REQ-VISION-UNIFY-002) so the user always gets a reply. A logged validation error surfaces the unknown field for prompt tuning. `plan.md` may opt for `extra="ignore"` if the operational signal turns into noise — decision recorded there. |
| R5 | **Non-apparel images route to `ask_clarify` instead of `respond`** if `_is_weak_vision` is wrong about which rule fires. | Medium | Medium | REQ-VISION-WEAKVISION-001 explicitly tests the `isApparel=False` path and asserts it routes to `respond`. The "no silent dead end" invariant (SPEC-AGENT-001 REQ-COMPAT-004) is structurally enforced regardless. |
| R6 | **Picker selection loses item context** if `pick_item.py` does not project `VisionItem` into the new state field correctly. | Medium | High | REQ-VISION-STATE-003 requires explicit assertions on `vision_selected_item.subcategory` / `fit` / `colorFamily` / `searchQueryKo` after a callback test. The legacy `vision_item` / `vision_keywords` fields stay populated for one release as a safety net (REQ-VISION-COMPAT-003). |
| R7 | **Sparse query collisions**: `enhance_query` (SPEC-PIPELINE-001) was tuned against the keyword-list path; `searchQueryKo` may shift its behavior. | Medium | Medium | Parity tests (REQ-VISION-PARITY-*) compare outcome quality before/after on a fixture set. `enhance_query`'s feature flag is unchanged; if the new query confuses it, the flag stays off and the natural-text query reaches Supabase directly. |
| R8 | **`SessionState` schema bump corrupts in-flight sessions** (in-memory store today, but the model still has to round-trip). | Low | Low | New fields all have safe defaults. A unit test exercises a "load v1 session, write v2 fields, read v2 fields" round-trip. The in-memory store has no persistence — restart resets everything; the risk is bounded to a single bot restart window. |
| R9 | **Multilingual Korean prompt content** (`searchQueryKo`, `sensitivityTags`) renders poorly under the bot's `BOT_LANGUAGE=en` reply policy if `respond` accidentally surfaces it. | Low | Low | `respond` and `ask_clarify` are unchanged by this SPEC; they continue to obey SPEC-MSG-001 REQ-MSG-005 (English replies). Korean content is internal-only (search query, metadata). A test asserts `respond`'s output never contains non-ASCII letters. |
| R10 | **Verbatim prompt copy includes JS-style template-literal syntax** (`${buildNodeReference()}`) that does not evaluate in Python. | Low | High if missed | REQ-VISION-UNIFY-001 explicitly requires that the `${...}` slots be replaced with their static evaluated content at copy time, and that `vision_prompt.py` carries a unit-test-checked smoke sentence to catch unfilled slots. |
| R11 | **Tests assume `extract()` returns `dict`** and break when the return type becomes `VisionResult`. | Medium | Low | REQ-VISION-COMPAT-001 explicitly permits rewriting test assertions; the fix is mechanical. The `model_dump()` escape hatch lets one-line-fix tests stay in dict form during the transition. |
| R12 | **`_is_weak_vision` rule churn** causes more `ask_clarify` fires (or fewer) than today, surprising users. | Medium | Medium | The new predicate is parameterized via env vars (REQ-VISION-WEAKVISION-001) so production tuning does not require a code change. Langfuse trace metadata (REQ-VISION-OBSV-001) records the predicate inputs so post-migration analysis can recalibrate. |
| R13 | **Recommendation-port DTO bump cascades** to `portal/app`'s direct `/recommend` callers if `RecommendRequest` schema changes. | Low | High if missed | REQ-VISION-SEARCH-002 keeps new fields strictly OPTIONAL on `RecommendRequest`; `portal/app`'s caller continues to send the existing payload unchanged. A contract test against the existing portal/app fixture guards this. |
| R14 | **Langfuse metadata size grows** with new fields, hitting trace size limits. | Low | Low | The new keys add ≤ 1 KB per trace. `mood_tags` is capped at 5 entries. Trace size is monitored on the Langfuse host. |

---

## Exclusions (What NOT to Build)

The following are explicitly out of scope for SPEC-VISION-UNIFY-001 and
MUST NOT be implemented as part of this SPEC:

1. **Migrating Vision into a shared microservice (Option C).** Two
   in-process Vision call sites remain. Microservice extraction is a
   future SPEC contingent on the prompt stabilizing in both channels.
2. **Calling portal/app's `/api/analyze` from the bot (Option B).** No
   cross-deployment HTTP dependency between the bot and Next.js.
3. **Modifying portal/app's `analyze.ts` prompt or schema.** Strict
   one-way convergence: portal/ai matches portal/app. Future schema
   changes start in portal/app.
4. **Changing the Vision model.** GPT-4o-mini via LiteLLM remains the
   backend. Only `max_tokens`, `temperature`, timeout, and the prompt
   body change.
5. **Self-critique loop, episodic memory, multi-image / outfit composition,
   tool-calling agents.** Tracked in separate SPECs.
6. **Streaming Vision output, multi-turn Vision context, Vision tool
   calls.** Single-shot JSON extraction, same as today.
7. **Replacing or modifying SPEC-MSG-001, SPEC-AGENT-001, or
   SPEC-PIPELINE-001.** Their requirements are extended through additive
   plumbing only.
8. **Changing `enhance_query`'s feature flag default or behavior.**
   SPEC-PIPELINE-001's flag remains as-is. The new `searchQueryKo` is
   passed through to `RecommendRequest`; what `enhance_query` does with
   it is governed by SPEC-PIPELINE-001.
9. **A separate Vision configuration UI / runtime override.** All
   tuning is via env vars.
10. **Persisting `VisionResult` to a database** beyond the in-memory
    `SessionStore`. Long-term storage is deferred.
11. **Translating `respond` / `ask_clarify` replies into Korean.** Reply
    language remains English (SPEC-MSG-001 REQ-MSG-005). Korean content
    is internal-only (search query, metadata).
12. **Re-tuning `enhance_query` against the new sparse query input.**
    Done in a follow-up if measurement shows degradation.
13. **Adding new picker UI affordances** (color swatch, fit badge) based
    on the new fields. UI parity with portal/app is out of scope; this
    SPEC is data-plumbing only.
14. **A side-by-side schema-diff dashboard.** The CI / weekly diff check
    in REQ-VISION-UNIFY-001 is the only drift-detection mechanism.
15. **Removing the legacy `vision_item` / `vision_keywords` session
    fields.** Deferred to a follow-up SPEC after the deprecation window.
16. **Removing `VISION_SCHEMA_V2` flag.** Deferred to a follow-up SPEC
    once the flag has stabilized at `True`.
17. **Group chats, channels, payments, Stars, Mini Apps.** Inherited 1:1
    DM scope from SPEC-MSG-001.

---

## Open Questions (to resolve during plan.md / implementation)

These do not block SPEC approval but should be answered before code is
written:

1. **Where does the verbatim prompt live?** REQ-VISION-UNIFY-001 mandates
   `app/channels/vision_prompt.py` with the prompt as a Python string
   constant. `plan.md` decides whether the file additionally exposes the
   evaluated `STYLE_NODE_IDS`, `SENSITIVITY_TAGS`, and the enum
   reference text as Python lists / dicts (useful for validation), or
   whether the prompt is a single opaque blob.
2. **CI drift check mechanism.** Manual diff at PR review, weekly bash
   diff (`diff portal/app/.../analyze.ts portal/ai/.../vision_prompt.py
   --ignore-all-space`), or a pre-commit hook. `plan.md` chooses based
   on team conventions.
3. **Pydantic `extra` policy.** `extra="forbid"` (fail fast on prompt
   drift) vs `extra="ignore"` (resilient against minor prompt updates).
   `plan.md` decides; the SPEC currently mandates `forbid` and accepts
   the operational cost.
4. **Exact mapping of outfit-level fields onto `RecommendRequest`.**
   `AnalyzedItem` has `subcategory` and `color_family`, but no
   `fit` / `fabric` / outfit-level fields. `plan.md` chooses between
   (a) extending `AnalyzedItem` with optional fields, (b) extending
   `RecommendRequest` with sibling outfit context, or (c) packing them
   into a `metadata` dict consumed by `enhance_query`. SPEC-PIPELINE-001
   may need a touch-up — `plan.md` confirms.
5. **`_VISION_TIMEOUT` value.** REQ-VISION-UNIFY-003 floors it at
   `30s` but the actual P95 of `run-vision.ts` may permit a smaller
   value. `plan.md` measures and pins.
6. **Legacy env-var deprecation timeline.** `ASK_CLARIFY_MIN_DESC_TOKENS`
   and `ASK_CLARIFY_AMBIGUOUS_LABELS` are read for one release.
   `plan.md` defines "one release" precisely (e.g., until the next
   tagged version, or until the rollback flag is removed).
7. **Should `vision_outfit_mood_tags` be the top-N by score or the
   raw list?** REQ-VISION-STATE-001 says "top labels" and REQ-VISION-OBSV-001
   says "max 5". `plan.md` makes the choice consistent across both.
8. **Whether `respond` should be aware of the new fields.**
   `vision_outfit_style_node_primary` etc. are available on
   `WorkingState` after this SPEC; whether `respond`'s prompt actually
   references them (e.g., "I see you're going for a {styleNode}
   vibe...") is left as a soft enhancement, decided in `plan.md`. The
   SPEC itself does NOT require behavioral changes in `respond`.

---

## Future Scope (post-MVP, separate SPEC)

- **Microservice extraction (Option C).** A shared Vision service called
  by both portal/app and portal/ai. Removes the schema-drift risk
  structurally.
- **Removing the `VISION_SCHEMA_V2` flag.** Lands once the flag has
  defaulted to `True` for at least one release with no rollbacks.
- **Removing legacy `vision_item` / `vision_keywords` session fields.**
  Lands once all read sites are migrated and the deprecation window
  closes.
- **`respond`-side use of outfit context.** Lets the bot reference
  styleNode / mood / detected gender naturally in replies (e.g.,
  "Got it — leaning street-minimal for fall.").
- **Picker UI parity with portal/app.** Show color swatch, fit badge,
  styleNode chip on the carousel cards.
- **Multi-image / outfit composition (H3).** Multiple inbound images
  per turn produce a fused `VisionResult` with cross-item coherence.
- **Re-tuning `enhance_query` against the new sparse input.** If
  measurement shows regression, SPEC-PIPELINE-001 gets a follow-up.
- **Vision result persistence beyond the in-memory session.** DB-backed
  `VisionResult` history per user → enables long-horizon style
  understanding.

---

## Cross-References

- **Builds on**:
  - SPEC-MSG-001 (Telegram channel transport — kept unchanged).
  - SPEC-AGENT-001 (LangGraph topology — extended through state +
    routing predicate).
  - SPEC-PIPELINE-001 (search pipeline — receives the rich query
    fields; behavior governed there).
- **Source of truth (read-only)**:
  - `portal/app/src/lib/prompts/analyze.ts` —
    `ANALYZE_SYSTEM_PROMPT`.
  - `portal/app/src/lib/analyze/run-vision.ts` — LiteLLM call
    parameters (`max_tokens=2500`, `temperature=0.3`).
- **Affected modules in portal/ai**:
  - `app/channels/vision.py`,
    `app/channels/vision_prompt.py` (NEW),
    `app/channels/session.py`,
    `app/channels/recommendation.py`.
  - `app/graphs/state.py`,
    `app/graphs/routing.py`,
    `app/graphs/nodes/vision.py`,
    `app/graphs/nodes/pick_item.py`,
    `app/graphs/nodes/search.py`.
  - `app/models/request.py` (potential touch-up — see Open Question 4).
  - `app/observability/langfuse.py` (metadata expansion only — no API
    change).
  - `app/core/config.py` (new env vars).
- **Tests**:
  - `tests/test_graph_nodes/test_vision.py`,
    `tests/test_graph_nodes/test_pick_item.py`,
    `tests/test_graph_nodes/test_search.py`,
    `tests/test_graph_flows.py`,
    `tests/test_recommendation_port.py`,
    plus a new `tests/test_vision_schema_parity.py` for
    REQ-VISION-PARITY-*.
- **Project context**: `/Users/hansangho/Desktop/portal/ai/CLAUDE.md`.
- **PR baseline**: SPEC-AGENT-001 (PR #11, commit `f0a7f03`) introduced
  the LangGraph topology and `app/graphs/nodes/vision.py` whose output
  shape this SPEC enriches.

---

## Definition of Done (P0)

- [ ] REQ-VISION-UNIFY-001 through REQ-VISION-UNIFY-003 implemented and
      acceptance criteria verified.
- [ ] REQ-VISION-STATE-001 through REQ-VISION-STATE-003 implemented and
      acceptance criteria verified.
- [ ] REQ-VISION-SEARCH-001 through REQ-VISION-SEARCH-003 implemented
      and acceptance criteria verified — `searchQueryKo` reaches
      `RecommendRequest.item.search_query_ko` byte-for-byte.
- [ ] REQ-VISION-WEAKVISION-001 implemented; all 5 rules covered by
      tests; non-apparel routes to `respond`, weak-but-apparel routes
      to `ask_clarify`.
- [ ] REQ-VISION-COMPAT-001 through REQ-VISION-COMPAT-005 implemented;
      `VISION_SCHEMA_V2=false` cleanly reverts to pre-migration behavior.
- [ ] REQ-VISION-OBSV-001 and REQ-VISION-OBSV-002 implemented;
      Langfuse `vision_extract` span carries the documented metadata
      keys; per-item logger output shows
      `subcategory/fit/colorFamily — searchQueryKo` preview.
- [ ] `app/channels/vision_prompt.py` exists with the verbatim copy of
      `ANALYZE_SYSTEM_PROMPT`; the file's docstring lists the source
      path and freeze date.
- [ ] `app/core/config.py` and `.env.example` declare
      `VISION_SCHEMA_V2`, `VISION_MAX_TOKENS`, `VISION_TEMPERATURE`,
      `VISION_TIMEOUT_S`, `ASK_CLARIFY_MIN_QUERY_TOKENS`,
      `ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES` with documented defaults.
- [ ] An end-to-end manual test against the dev Telegram bot exercises:
      (a) photo with multi-item outfit → picker → tap → re-search; the
      logged `searchQueryKo` for the selected item matches the value
      portal/app produces for the same image.
      (b) non-apparel photo → polite reply (no `ask_clarify`).
      (c) ambiguous-but-apparel photo → `ask_clarify` fires.
      (d) `VISION_SCHEMA_V2=false` restart → minimal schema, legacy
      behavior, no regression.
- [ ] Snapshot parity test compares portal/app's `analyze.ts` output and
      portal/ai's new `vision.extract` output on a shared fixture
      image: JSON keys match (values may differ within tolerance).
- [ ] Existing Telegram E2E test (image upload → results card) passes
      with no behavior regression.
- [ ] `ruff check . && ruff format --check .` passes.
- [ ] `pytest -q` passes.
