## SPEC-ARCH-AI-001 Progress

- Started: 2026-05-16 (isolated worktree, base dev 5ff58a3)
- Mode: DDD. Scope this run: ANALYZE -> PRESERVE (characterization tests green) -> STOP at gate. IMPROVE deferred.

### PRESERVE phase complete — 2026-05-16

Characterization test net created (REST `/recommend` scope; channel post-filter
out of scope per user decision). Zero `app/` source changes.

**Test files created:**
- `tests/test_arch_ai_001/__init__.py` (package marker / net charter)
- `tests/test_arch_ai_001/conftest.py` (shared fixtures: `fixed_embed` 768-dim
  `[0.1234567]*768`, `rpc_capture`, `patch_rpc` factory, autouse
  `_enhance_disabled`; `RPCCapture` helper reusing the proven
  `tests/test_pipeline_with_enhance.py` monkeypatch seams)
- `tests/test_arch_ai_001/test_run_pipeline_characterization.py` (Net 1 — 9 tests)
- `tests/test_arch_ai_001/test_diversify_characterization.py` (Net 2 — 11 tests)
- `tests/test_arch_ai_001/test_search_rpc_contract_characterization.py` (Net 3 — 6 tests)

**Scoped gate result (green criterion):**
`uv run pytest tests/test_arch_ai_001/ tests/test_pipeline_with_enhance.py tests/test_enhance_query.py -q`
→ **46 passed** (26 new + 6 existing pipeline + 14 existing enhance_query; no
existing-test regression). `ruff check` + `ruff format --check` on
`tests/test_arch_ai_001/` → all clean.

**Zero app/ source changes (proof):**
`git diff --stat` → empty. `git status --short` → only untracked
`tests/test_arch_ai_001/` and `.moai/specs/SPEC-ARCH-AI-001/`.

**Current behavior locked as golden (observe-then-lock surprises):**
- `_tolerance_to_target_count` uses Python `round()` (banker's): `0.05 → 10.5
  → 10` (even), `0.15 → 11.5 → 12` (even). Locked in Net 2 Case 4.
- diversify drop asymmetry: under `brand_filter=["uniqlo"]` (brand_cap 6) the
  golden survivor is **p10 not p12** — the `market` platform cap (3) fills at
  p10 before p12 is iterated. This ordering is the exact arithmetic IMPROVE
  must preserve byte-identically.
- runner Candidate map: missing `score` → `0.0` (locked via p7); missing
  `brand` key → `""` (locked via p9 — note: key must be ABSENT, a present
  `brand: None` would fail Pydantic `brand: str`).
- `query_embedding` pgvector format: `f"{0.1234567:.7f}"` → `"0.1234567"`
  with no rounding/padding; full 768-element string locked byte-for-byte.
- PIPELINE_PARALLEL_ENABLED True vs False → byte-identical response (locked).

STOP — IMPROVE (service/repository extraction) deferred to a later run.

### IMPROVE phase — PR1 (service layer extraction) complete — 2026-05-17

Pure extraction (code movement), zero behavior change. `app/services/` layer
created; `app/pipeline/` kept as thin re-export shims (REQ-AI-001, PR1 of 6).

**Files created:**
- `app/services/__init__.py` — package re-exports
- `app/services/diversify_service.py` — cap/tolerance/order arithmetic moved
  VERBATIM from old `app/pipeline/diversify.py` (incl. `tolerance_to_target_count`
  banker's rounding `int(round(10 + t*10))` — unchanged)
- `app/services/search_service.py` — search orchestration + 3-tier query_text
  selection moved verbatim; RPC call STAYS (dispatched via
  `app.pipeline.search.SupabaseProvider.rpc` to honor the monkeypatch seam;
  repository relocation is PR2/REQ-AI-002)
- `app/services/embed_service.py` — embed body moved verbatim (seam:
  `app.pipeline.embed.EmbedProvider.embed_image_url`, resolved lazily at call
  time to break the shim<->service import cycle)
- `app/services/database_service.py` — pass-through wrapper over
  `SupabaseProvider` (no behavior change; DI is PR3/REQ-AI-003)

**Files modified (→ thin re-export shims, public names preserved):**
- `app/pipeline/diversify.py` (75→20 LOC) — re-exports `diversify_step`
  (`@observe`-decorated) + `_tolerance_to_target_count` alias
- `app/pipeline/search.py` (98→24 LOC) — re-exports `search_step`,
  `_embedding_to_pgvector`, `SupabaseProvider` (patch seam)
- `app/pipeline/embed.py` (25→23 LOC) — re-exports `embed_step`,
  `EmbedProvider` (patch seam)

**Gate result:**
`uv run pytest tests/test_arch_ai_001/ tests/test_pipeline_with_enhance.py tests/test_enhance_query.py -q`
→ **46 passed** (same 46 as PRESERVE). `git diff a8eae03 -- tests/test_arch_ai_001/`
→ empty (zero golden literal change, no test edited). `ruff check` +
`ruff format --check` on `app/services/ app/pipeline/` → all clean.

**Behavior-equivalence risks resolved:**
- Monkeypatch seam preservation: conftest + `test_pipeline_with_enhance.py`
  patch `app.pipeline.embed.EmbedProvider.embed_image_url` and
  `app.pipeline.search.SupabaseProvider.rpc` by string. Both seam symbols are
  re-exported on the shim modules and the actual call is resolved at call time
  via the shim module object → patch effective, byte-identical.
- Circular import (shim ↔ service): broken by lazy `import app.pipeline.{embed,
  search}` inside the service function body (call time only).
- diversify arithmetic: diffed old body vs `diversify_service` — only the
  function-name renames differ; the cap loop, `int(round(...))`, break-on-target
  and all log lines are byte-identical.
- Pre-existing full-suite failures (9: `test_critique_loop.py`,
  `test_observability/`) verified present on baseline a8eae03 (langfuse v3 /
  pydantic v1 env issue) — NOT introduced by PR1.

STOP — PR2-6 (repository / di / memory / domain / contract) deferred to later runs.
