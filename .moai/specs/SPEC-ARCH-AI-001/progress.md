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

### IMPROVE phase — PR2 (SearchRepository) complete — 2026-05-17

Pure extraction, zero behavior change. `app/infrastructure/repositories/`
created; the `search_products_v5` RPC name + param-dict mapping now live in
EXACTLY ONE place — `SearchRepository` (REQ-AI-002, PR2 of 6).

**Files created:**
- `app/infrastructure/__init__.py`
- `app/infrastructure/repositories/__init__.py` (re-exports `SearchRepository`)
- `app/infrastructure/repositories/search_repository.py` — `_RPC_NAME =
  "search_products_v5"` (sole literal) + `SearchRepository.build_params` (the
  param dict moved VERBATIM from search_service) + `SearchRepository.search`
  (dispatches via `app.pipeline.search.SupabaseProvider.rpc` seam) +
  `embedding_to_pgvector` (moved here, co-located with the param map)

**Files modified:**
- `app/services/search_service.py` — no longer references the RPC name nor
  builds the param dict; delegates to `SearchRepository.build_params` +
  `SearchRepository.search`. Diagnostic log text byte-identical. Back-compat
  `embedding_to_pgvector` re-export preserved for the pipeline shim.

**Gate result:**
46 passed (same 46). `git diff a8eae03 -- tests/test_arch_ai_001/` empty
(zero golden change). Net(3) 6/6 param-mapping tests green -> captured
`(fn_name, params)` byte-identical. ruff clean. `import app.main` OK.

STOP — PR3-6 (di / memory / domain / contract) deferred to later layers.

### IMPROVE phase — PR3 (DI container) complete — 2026-05-17

Pure extraction, zero behavior change. `app/core/di.py` FastAPI Depends
container created; `app/providers/db_pool.py` `get_pool`/`get_loop` are now
thin delegating adapters to it (REQ-AI-003, PR3 of 6).

**Files created:**
- `app/core/di.py` — `provide_settings` (reuses config.get_settings
  lru_cache singleton, no re-instantiation) / `provide_db_pool` /
  `provide_db_loop` (read db_pool's `_pool`/`_loop` globals directly so
  state ownership + string monkeypatches + `db_pool._pool` reads stay
  byte-identical, no delegation cycle) / `provide_embed_provider`.

**Files modified:**
- `app/providers/db_pool.py` — `get_pool`/`get_loop` delegate to
  `di.provide_db_pool`/`provide_db_loop`. All other public symbols
  (`init_pool`, `close_pool`, `run_in_pool_loop`, `reset_pool_for_test`,
  `_sanitize_dsn`, `_pool`/`_loop` state) UNCHANGED. RuntimeError messages
  byte-identical. Pool state stays in db_pool's namespace (NOT moved) so
  `db_pool._pool` (main.py conv-log probe) + `monkeypatch.setattr(
  "app.providers.db_pool.get_pool", ...)` (test_conversation_log) keep
  working.

**Gate result:**
46 passed (scoped); golden diff empty. Full collectable suite: 632 passed,
90 skipped, 9 pre-existing failures only (test_critique_loop /
test_observability — langfuse v3/pydantic env), ZERO new. `import app.main`
OK. ruff clean on `app/core/ app/providers/db_pool.py`.

STOP — PR4-6 (memory / domain / contract) deferred to later layers.

### IMPROVE phase — PR4 (memory relocation) complete — 2026-05-17

Pure module move, zero logic change. `session.py`/`session_pg.py`/
`taste_profile.py`/`taste_profile_pg.py` relocated `app/channels/` ->
`app/infrastructure/memory/` (REQ-AI-004, PR4 of 6, highest blast — 57 sites).

**Files moved (git mv, content verbatim):**
- `app/channels/session.py` -> `app/infrastructure/memory/session.py`
- `app/channels/session_pg.py` -> `app/infrastructure/memory/session_pg.py`
- `app/channels/taste_profile.py` -> `app/infrastructure/memory/taste_profile.py`
- `app/channels/taste_profile_pg.py` -> `.../memory/taste_profile_pg.py`

**Internal cross-imports rewritten to canonical path (only change in moved
files):** `session_pg` `from app.channels.session` ->
`from app.infrastructure.memory.session`; `taste_profile_pg`
`from app.channels.taste_profile` -> `.../memory.taste_profile`.
`app.channels._jsonable` import unchanged (not relocated). ruff I001
import-block re-sort applied to session_pg (ordering only, no behavior).

**Shims (sys.modules alias — fully transparent):**
The 4 old `app/channels/` paths `sys.modules[__name__] = _canonical` so the
old import path IS the same module object. This was REQUIRED:
`tests/test_implicit_feedback/conftest.py` monkeypatches the private
`app.channels.taste_profile._store` global — a star-re-export shim (first
attempt) created a separate namespace and broke 7 tests with
AttributeError. The sys.modules alias makes every attribute (private state
included), class identity, and import resolve byte-identically.

**Gate result:**
46 passed (scoped); golden diff empty. Full collectable suite: 632 passed,
90 skipped, 9 pre-existing failures ONLY (test_critique_loop /
test_observability), ZERO new failures/errors (verified the 7 transient
implicit_feedback errors from the rejected star-shim are fully resolved).
`import app.main` OK. ruff clean.

STOP — PR5-6 (domain / contract) + PR-final deferred to later layers.

### IMPROVE phase — PR5 (domain/DTO split) complete — 2026-05-17

Additive scaffolding, zero behavior change. Internal domain layer +
shared type aliases introduced, separate from the Pydantic transport DTOs
in app/models/ (REQ-AI-005, PR5 of 6).

**Files created (additive only — NO existing file modified):**
- `app/core/types.py` — framework-agnostic aliases (`RpcRow`, `CountMap`,
  `LatencyMap`).
- `app/domain/__init__.py` — re-exports the domain model.
- `app/domain/search.py` — `SearchCandidate` (frozen slots dataclass) +
  `search_candidate_from_row` mapper applying the EXACT field coercions the
  runner uses inline today (missing score -> 0.0, absent brand/name -> "",
  id -> str). Verified equivalent at runtime.

**Behavior preservation:** `app/models/` (RecommendResponse/Candidate
Pydantic DTOs) and `app/pipeline/runner.py` serialization path are
UNTOUCHED — the Net(1) response snapshot is byte-identical. The domain
layer is the additive seam for SPEC-SEARCH-UNIFY-001 v6 to evolve internal
types independently; it is not yet wired into the hot path (wiring would
risk serialization drift against the HARD Net(1) lock).

**Gate result:**
46 passed; golden diff empty (zero change). ruff clean on
`app/domain/ app/core/types.py`. `import app.main` OK.

STOP — PR6 (RPC contract validation) + PR-final deferred.

### IMPROVE phase — PR6 (RPC contract validation) complete — 2026-05-17

Happy path byte-identical; ONE new error branch added (the only layer
permitted to). REQ-AI-006, PR6 of 6.

**Files created:**
- `app/infrastructure/repositories/search_rpc_contract.py` —
  `SearchRpcRowContract` (Pydantic, PERMISSIVE: only `id` required, all
  else optional, `extra="allow"`; accepts every shape the pre-PR6 runner
  accepted incl. absent score/brand, id int|str) + `RpcContractError`
  (structured: row_index + detail) + `validate_rpc_rows` (returns the
  ORIGINAL rows untouched on success — no coercion -> happy path identical).
- `tests/test_arch_ai_001/test_rpc_contract_drift.py` — NEW net file (Net 4,
  5 tests). Does NOT edit any of the 3 existing golden nets/conftest.

**Files modified:**
- `app/infrastructure/repositories/search_repository.py` — `search()` now
  calls `validate_rpc_rows(rows)` after the RPC, before returning (= before
  scoring/diversify). Returns original rows on success.

**Gate result:**
51 passed (original 46 ALL still pass + 5 new drift tests). `git diff
a8eae03` of the 5 existing net files (3 tests + conftest + __init__) EMPTY
— zero edit to existing golden, new file only. Full collectable suite: 637
passed (632 baseline + 5 new), 90 skipped, 9 pre-existing failures ONLY,
zero new. `import app.main` OK. ruff clean.

STOP — PR-final (shim removal) deferred.
