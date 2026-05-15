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
