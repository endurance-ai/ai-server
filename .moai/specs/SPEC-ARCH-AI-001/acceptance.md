---
id: SPEC-ARCH-AI-001
artifact: acceptance
created: 2026-05-17
---

# SPEC-ARCH-AI-001 — Acceptance (Characterization Nets)

The PRESERVE characterization net (`tests/test_arch_ai_001/`, committed a8eae03)
is the byte-identity oracle for the IMPROVE extraction. Golden literals are
IMMUTABLE — if an extraction fails an assertion, the extraction is wrong, never
the test (REQ-AI-007).

## Net 1 — `run_pipeline` end-to-end response snapshot

`test_run_pipeline_characterization.py`

- **Given** a `RecommendRequest` and a fixed 768-dim embedding
  (`EmbedProvider.embed_image_url` patched) plus a fixed hand-authored 14-row
  RPC payload (`SupabaseProvider.rpc` patched), enhance_query disabled.
- **When** `run_pipeline(req)` runs the full embed → enhance → search →
  diversify path under tolerance {0.0, 0.5, 1.0}, final_limit {None, 5},
  brand_filter {None, ["uniqlo"]}, and `PIPELINE_PARALLEL_ENABLED` {True, False}.
- **Then** `resp.item_id`, the ordered `(id, brand, score, dense_rank,
  sparse_rank)` result tuples, and the full `resp.counts` dict equal the locked
  golden; `resp.latency_ms` key set is exactly
  `{embed, enhance_query, search, diversify}` (values are wall-clock,
  non-deterministic — keys only). Parallel and sequential paths are
  byte-identical.

## Net 2 — `diversify_step` cap / tolerance / order

`test_diversify_characterization.py`

- **Given** a `PipelineState` with hand-built `raw_candidates` and default
  settings (`SEARCH_BRAND_CAP=2`, `SEARCH_PLATFORM_CAP=3`).
- **When** `diversify_step(state)` runs across: brand cap, brand_filter cap
  widening (×3 → 6), platform cap, tolerance→target_count incl. banker's
  rounding (0.05→10, 0.15→12), final_limit override, blank/None brand bucket
  collapse, and mid-iteration break.
- **Then** the exact surviving `id` order and the full `counts`
  (`after_diversify`, `final`) equal the locked golden. The
  `int(round(10 + clamp(t,0,1)*10))` banker's rounding is asserted explicitly
  and must stay exact.

## Net 3 — `SupabaseProvider.rpc("search_products_v5", ...)` param mapping

`test_search_rpc_contract_characterization.py`

- **Given** a `RecommendRequest` (with/without price_filter, brand_filter,
  ko/en query) and the fixed embedding.
- **When** `run_pipeline(req)` reaches the search step (RPC patched to capture).
- **Then** the captured `fn_name` is `"search_products_v5"` and the full
  `params` dict matches byte-for-byte, including the `:.7f` pgvector string
  (`[0.1234567,...]` × 768), the 3-tier `query_text` fallback
  (search_query_ko > search_query), `k=50`, `rrf_k=60`, and all hard filters
  pinned to `None`. This anchors REQ-AI-002 (PR2 moves the RPC name + param
  mapping into `SearchRepository`; this net proves the move is byte-identical).

## Gate command

```
uv run pytest tests/test_arch_ai_001/ tests/test_pipeline_with_enhance.py tests/test_enhance_query.py -q
```

PASS = 46 passed, zero golden literal change, no test edited.
