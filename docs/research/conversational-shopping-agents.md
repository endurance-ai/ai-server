# Conversational Shopping Agents — Research Synthesis

> **Purpose**: inform the redesign of `kikoai/ai`'s Telegram fashion bot (currently a 7-state SM in `app/channels/scenario.py`) toward a more natural, extensible conversation model.
>
> **Date**: 2026-05-05
> **Caveat on noscroll.com**: The user cited noscroll.com as primary inspiration. Investigation revealed noscroll.com is a **SMS news-digest bot**, not a fashion product. We treat it as a *philosophy* reference (text-only, sample-first, negatives matter, conversation-as-UI), not a feature blueprint. Pending user confirmation that this interpretation is correct.

---

## TL;DR — five takeaways

1. **Don't jump from SM → full agent loop.** The right next step is **Routing-LLM** (Tier 2 in Anthropic's "Building Effective Agents" taxonomy): one LLM call classifies the inbound message into typed intents, then dispatches to deterministic handlers. Klarna's production assistant runs at this tier — not full ReAct.

2. **Tap-critique on result cards is table stakes.** Pinterest (region-tap), Google Lens (circle), and ChatGPT Shopping ("More like this" / "Not interested" buttons) all converge on this. Natural-language-only refine (Klarna, Perplexity) gets used less per reviews. **Our biggest UX gap.**

3. **Critique is the highest-leverage academic pattern we're not using.** Unit critiques ("cheaper") + compound critiques ("less denim, more streetwear under $80") are well-studied; LLM-era twist is to let the model propose 2-3 compound-critique chips based on result variance. Cuts conversation length ~40% vs naive re-asks.

4. **Persistent memory is now baseline.** Klarna, ChatGPT Shopping, Perplexity Pro all bind taste to account. Stateless-per-session (our current model) reads as 2022. Three-layer split: working memory (per-conversation) + long-term taste profile (per-user) + implicit feedback capture (every interaction is a label).

5. **Latency is a UX problem, not just an infra problem.** ChatGPT's explicit "researching for a few minutes, X sources reviewed" outperforms generic spinners. Pre-search acknowledgement ("looking for beige knits under $80, right?") doubles as confirmation + masks 3-8s waits.

---

## Section 1 — noscroll.com (philosophy reference only)

> noscroll.com is **not a fashion product**. It is a text-message news-digest AI bot (Nadav Hollander, ex-OpenSea CTO; launched April 2026; $9.99/mo). Tagline: *"noscroll monitors the situation — so you don't have to."*

### What transfers (the philosophy)

- **Sample-before-commit.** Onboarding produces a *real* sample digest as the trial artifact. Fashion equivalent: first uploaded image returns a fully-curated card set before asking the user to commit any preferences.
- **Negative preferences are turn-1 questions.** Onboarding asks *"what kind of news you want to hear about, and what you're not interested in"* (verbatim). Most recommenders ignore exclusions; noscroll bakes them in. Equivalent: ask brands/styles to avoid, not just brands to like.
- **Implicit signal mining at start.** X likes/follows/bookmarks bootstrap taste. Equivalent: prior swipes / IG saves / cached vision results.
- **Each delivered item is a conversation hook.** *"Reply anytime to dig in, adjust, ask questions"* (verbatim). Cards as endpoints kill the loop.
- **Cadence is negotiated, not defaulted.** "Weekly for casuals, multiple/day for junkies."
- **Zero-UI surface (controversial).** No buttons, no carousels — pure prose. Worth A/B'ing against InlineKeyboard chips; per Section 2 evidence, tap-critique generally wins for refine.

### What we can't tell from outside

Real SMS transcripts, exact tone, failure-state copy, memory editability — all unverified. The 7-day trial is the only path.

---

## Section 2 — comparable shopping agents

### Comparison table

| Service | Input | Refine UX | Memory | Distinctive |
|---|---|---|---|---|
| **Pinterest Lens** | Image + tap-region on Pin | Tap glowing item → drag crop; body-type filter chips | Implicit taste graph | Region-tap on a Pin makes any sub-object the new query |
| **Klarna AI Assistant** | Text + Shopping Lens (image→text) | Free-flow NL ("cheaper", "for skiing") | Persistent (Klarna account) | **Price history graph** inline in chat |
| **Perplexity Shopping** | Text-first; Snap-to-Shop image | Conversational follow-up (no tap-critique) | Pro-account; weak cross-thread | **Pros/Cons block + 1-click checkout** |
| **Google Lens** | Image + circle-region + text overlay | Circle to Search anywhere; add words ("brown", "velvet") | Account-level history; no surfaced taste | **Circle to Search at OS level** (no app-switch) |
| **ChatGPT Shopping** | Text; image attach | "More like this" / "Not interested" buttons + chat | **Aggressive cross-session memory** | Agent **asks clarifying Qs BEFORE searching**; multi-minute deep research |

### Cross-cutting patterns (likely table stakes)

1. **Tap-critique on cards** — Pinterest, Lens, ChatGPT. Faster than free-text refine.
2. **Image → text translation as normalization** — Klarna does it explicitly; others hide it.
3. **Price/price-history surfacing** — fashion bots that omit feel toy-like.
4. **Persistent memory bound to account/identity.**
5. **Region-as-query** beats whole-image upload for refine.

### Anti-patterns (what to avoid)

1. **Hidden latency contracts.** Generic spinners → users abandon. ChatGPT's named latency ("a few minutes, here's progress") wins.
2. **Conversation that pretends to be search.** Perplexity's "single Q → committed answer" frustrates users who want to refine.
3. **Pure-AI overreach without human escape.** Klarna had to partially reverse 2024's AI-only push in 2025 ("lower quality" admission).

---

## Section 3 — engineering & academic patterns

### CRS architecture taxonomy (Anthropic-aligned)

| Tier | Pattern | What you get | What it costs |
|------|---------|--------------|---------------|
| 0 | Rule-based / slot-filling | Deterministic, debuggable | Brittle, no paraphrase |
| 1 | **Hard-coded SM** ← *our current* | Predictable, easy to reason | Branching explodes per new trigger |
| 2 | **Routing-LLM + deterministic handlers** | LLM does intent classification only | Need clean handler API + classifier eval set |
| 3 | Orchestrator-workers | LLM dynamically composes tool calls | Harder to test, cost grows per turn |
| 4 | Full ReAct agent | Open-ended Thought→Action→Observation | Overshoots simple tasks |

> **Anthropic's framing**: Tiers 2-3 are *workflows* (predefined code paths with LLMs at decision points). Tier 4 is the only true "agent." Their advice: stay at the lowest tier that works.

**Our case**: Tier 2 is almost certainly the right next step. The 7-state SM becomes a router that calls the same handlers we already have, plus new ones (critique, clarify, browse-more).

### Critique patterns (Chen & Pu foundations + LLM-era)

- **Unit critique**: one attribute at a time ("cheaper", "longer sleeves") → structured filter delta on top of last result set
- **Compound critique**: multiple attributes at once ("less formal, more streetwear, under $80") → either Apriori-mined or **LLM-proposed quick-reply chips based on result variance** ("These are mostly cropped — show longer fits?")
- **Concrete pattern for our case**: every result card carries an implicit critique surface
  ```
  [Card #3]  Inline: "More like this" / "Less denim" / "Cheaper" / "Show more"
  ```
- **"More like this"** doesn't need an LLM at all — embedding query anchored on that product's vector with brand-diversity penalty
- **Free-text critique** ("but in beige") → small LLM call → structured `{anchor_product_id, attribute_deltas: [...]}` → feeds `enhance_query`

### Memory architecture (3-layer)

1. **Working memory (per-conversation)** — last query, last result IDs, last critique, pending clarification. Already in `SessionStore`. Keep small + structured.
2. **Long-term taste profile (per-user)** — flat dict: `{liked_brands, disliked_brands, price_range_observed, style_keywords}`. Mergeable, cheap to inject into `enhance_query`.
3. **Implicit feedback capture** — every interaction is a label:
   - Card shown but not clicked within N turns → soft negative
   - "More like #3" → strong positive for #3's attributes
   - Re-asking similar query within session → previous set unsatisfying
   - Link click-through → strong positive

### Latency UX patterns (for Telegram constraints)

Telegram doesn't support token streaming, but supports `editMessageText` and `chat-action`:

1. Receive message → immediate `sendChatAction: typing` ✅ (we already do)
2. Send placeholder: "검색 중... (보통 3-5초)" ✅ (added in PR #9)
3. **NEW**: pre-search acknowledgement: "이런 스타일 찾고 계시는 거 맞죠? [요약된 쿼리]" — confirmation + latency mask
4. **NEW**: edit-in-place pattern for incremental result reveal
5. Chat-action heartbeat every 4-5s ✅ (added in PR #9)

### Error recovery patterns

- **Empty result set** → don't say "no results"; auto-relax constraints, label it ("정확한 매치가 없어서 비슷한 스타일로 찾았어요"). This is Anthropic's evaluator-optimizer in miniature.
- **Vague user input** → information-gain check: if top-1 confidence margin over top-10 < threshold, ask one clarifying question; otherwise just show + let them critique.
- **LLM hallucinates** → schema-validate every output (Pydantic); on validation failure, retry once with stricter prompt, then fall back to deterministic.

### Cost control

Routing classifier should be **small/fast model** (Haiku-class). Reserve bigger models for: vision extraction, critique parsing, query enhancement. Don't run an LLM on every message — gate by cheap deterministic checks first (URL? image? short keyword? — handle deterministically; only ambiguous text goes to LLM router).

---

## Section 4 — gap analysis vs current `scenario.py`

| Capability | Current | Industry baseline | Gap |
|---|---|---|---|
| Input modalities | Image URL only (bytes blocked) | Image + text + region-select | **High** — region-select missing; bytes blocked is OK for now |
| Refine mechanism | Single free-text intent → reuses all keywords | Tap-critique buttons + NL + chip suggestions | **Critical** — the #1 gap |
| Memory | Per-session in-memory only | 3-layer (session + taste + implicit) | **Critical** — no taste profile, no implicit feedback |
| Conversation flow | Linear 7-state SM | Routing-LLM + workflow handlers | **High** — new triggers explode SM |
| Latency UX | Typing heartbeat + start msg (PR #9) | + pre-search acknowledgement + edit-in-place | **Medium** |
| Error recovery | ZERO_RESULT message | Auto-relax + clarifying-question | **Medium** |
| Result presentation | 5 photo cards + closer | + critique buttons per card + price comparison | **High** |
| Result resumability | TTL-based session only | "Continue where you left off" w/ state replay | **Low-Med** |

---

## Section 5 — three redesign options

### Option L (Lite): "tap-critique + taste profile"

Stay with the SM, add the two highest-leverage missing pieces.

- **Tap-critique buttons per result card**: "More like this" / "Less of this" / "Cheaper" / "Different brand"
  - "More like this" = embedding query anchored on that product (no LLM)
  - "Cheaper" = price-filter delta (no LLM)
  - "Less of this" = recorded as negative signal, regenerate result set
- **Long-term taste profile**: flat `{liked_brands, disliked_brands, price_range, style_keywords}` per `from_user_id`, persisted (initially in same in-memory store, later Redis)
- **Implicit feedback capture**: log every card shown / clicked / dismissed
- Keep current `Trigger` enum; add `Trigger.CARD_CRITIQUE` and `Trigger.TASTE_UPDATE`
- **Effort**: ~2-3 days. **Latency cost**: zero (no new LLM). **Cost**: zero new model calls.
- **Limitations**: still can't handle paraphrase ("denim is too much" → not routed); still single-image flow.

### Option M (Medium): "Routing-LLM + critique workflow" ← *recommended*

Anthropic's Routing pattern. SM becomes a workflow graph.

- **Replace `classify_input`** with a small-model (Haiku-class) classifier that handles paraphrase and mixed intent. Outputs typed intent: `{new_search, critique, clarify_response, browse_more, off_topic, link_resolve, taste_update}`.
- **Critique handler**: small LLM call parses free-text critique into `{anchor_product_id?, attribute_deltas: [...]}`; deterministic search re-runs with deltas applied
- **LLM-proposed compound-critique chips**: after results sent, second small-model call generates 2-3 critique suggestions based on result variance ("These are all denim — show non-denim?")
- **All of Option L** (tap-critique + taste profile + implicit feedback)
- **Pre-search acknowledgement**: "찾는 거 정리하면: 베이지 톤 니트, 캐주얼, 5만원대 이하 — 맞아요?" before pipeline call
- **Effort**: ~5-7 days. **Latency cost**: +200-500ms per turn (router). **Cost**: ~$0.001-0.005 per turn (Haiku-class router).
- **Limitations**: still no multi-item outfits; still no cross-platform availability checks.

### Option H (Heavy): "Orchestrator-workers agent"

Full LLM-driven orchestration with parallel sub-tasks.

- **Tools as first-class citizens**: `search_products`, `refine_with_critique`, `get_taste_profile`, `update_taste_profile`, `ask_clarifying_question`, `extract_vision_items`, `compose_outfit`
- **LLM orchestrator** (Sonnet-class) decides per-turn which tools to call, in what order, with what params. Handles complex multi-step user requests: "outfit me from this image but swap the shoes for sneakers under $100"
- **Evaluator-optimizer loop**: after diversify, cheap LLM scores result quality; triggers re-search with adjusted params if below threshold
- **Memory tooling**: orchestrator can read/write taste profile mid-turn
- **All of Option M**
- **Effort**: ~2-3 weeks. **Latency cost**: +1-3s per turn (orchestrator + tool calls). **Cost**: ~$0.02-0.10 per turn.
- **Limitations**: harder to test, cost/latency creep, debugging is non-trivial. Premature for current usage volume.

### Recommendation matrix

| Criterion | L | M | H |
|---|---|---|---|
| Time to ship | ⭐⭐⭐ | ⭐⭐ | ⭐ |
| Latency | ⭐⭐⭐ | ⭐⭐ | ⭐ |
| Cost | ⭐⭐⭐ | ⭐⭐ | ⭐ |
| UX naturalness | ⭐ | ⭐⭐⭐ | ⭐⭐⭐ |
| Extensibility | ⭐ | ⭐⭐ | ⭐⭐⭐ |
| Debuggability | ⭐⭐⭐ | ⭐⭐ | ⭐ |
| Risk | Low | Med | High |

**Recommended: Option M.** Hits the critical UX gaps (tap-critique, taste profile, paraphrase handling) without taking on the orchestration complexity that current usage doesn't justify. Leaves a clean upgrade path to H if usage justifies it later.

---

## Section 6 — appendix (full agent reports)

The three sub-agent reports are preserved verbatim below for traceability.

### Appendix A — noscroll.com teardown (Agent 1)

*Available in commit history; key findings absorbed into Section 1.*

### Appendix B — comparable agents (Agent 2)

*Available in commit history; key findings absorbed into Sections 2 + 4.*

### Appendix C — engineering & academic patterns (Agent 3)

*Available in commit history; key findings absorbed into Sections 3 + 5.*

---

## Sources (consolidated)

**noscroll.com**
- noscroll.com landing
- TechCrunch (2026-04-23) — Meet Noscroll
- Yahoo Tech repost — Hollander quotes
- Fyself News — interaction details
- Progressive Robot — UX walkthrough

**Comparable shopping agents**
- Pinterest Lens help / Pinterest Engineering blog / TechCrunch body-type filter rollout / Search Engine Land
- Klarna press release / PYMNTS / PromptLayer (AI-first → hybrid pivot)
- Perplexity Shop blog / Intero Digital review / TechCrunch shopping launch
- Google Lens visual search blog / Style ideas blog / Glossy update
- OpenAI ChatGPT Shopping / OpenAI Help / Retail Dive / TechTimes

**Engineering & academic**
- arXiv 2502.10050 — LLM-powered Agents for Recommender Systems (survey)
- arXiv 2503.05659 — LLM Empowered Agents for Recommendation and Search
- arXiv 2510.12015 — Asking Clarifying Questions for Preference Elicitation (Google Research 2025)
- arXiv 2101.09459 — CRS: Advances and Challenges
- Chen & Pu — Critiquing-based recommenders (Springer)
- Anthropic — Building Effective Agents
- LangChain — Klarna AI Assistant on LangGraph
- Pragmatic Engineer — Klarna's AI chatbot critique
- Stitch Fix — Style Assistant + Algorithms Tour + implicit feedback
- Microsoft / Redis — streaming UX for LLM chat apps
