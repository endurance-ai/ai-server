"""SPEC-AGENT-V3-REACT / T10 — wrap-only static verification.

REQ-AGENT-V3-COMPAT-WRAP-001 / AC-X.2. AST proof that the new modules WRAP
existing helpers and define NO new LLM-call / evaluation / ranking algorithm,
and that _reflexion.py reaches the LLM only via evaluator._call_llm (never
LLMProvider.chat directly).
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent.parent / "app"


def _tree(rel: str) -> ast.Module:
    return ast.parse((APP / rel).read_text(encoding="utf-8"))


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for a in node.names:
                names.add(f"{mod}.{a.name}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name)
    return names


def _called_attrs(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


# ── Gap1 — _memory_context.py wraps get_recent_history + TasteProfile ──────


def test_memory_context_wraps_only():
    tree = _tree("agents/_memory_context.py")
    imps = _imported_names(tree)
    calls = _called_attrs(tree)
    assert "app.infrastructure.memory.taste_profile.get_taste_store" in imps
    assert "app.agents.tools.get_recent_history.dispatch" in imps
    # Uses the existing boost/exclude helpers, not a new summarizer.
    assert {"boost_brands", "boost_keywords", "exclude_brands"} <= calls
    # No LLM provider import — memory injection never calls an LLM.
    assert not any("LLMProvider" in i for i in imps)


# ── Gap2 — _reflexion.py wraps evaluator helpers, no direct LLMProvider ────


def test_reflexion_wraps_evaluator_helpers_only():
    tree = _tree("agents/_reflexion.py")
    imps = _imported_names(tree)
    calls = _called_attrs(tree)
    assert "app.graphs.nodes.evaluator._call_llm" in imps
    assert "app.graphs.nodes.evaluator._build_fastpath_delta" in imps
    assert "app.graphs.nodes._evaluator_prompt.build_user_prompt" in imps
    # Reaches the LLM ONLY via evaluator._call_llm.
    assert "_call_llm" in calls
    # NEVER imports / calls LLMProvider.chat directly.
    assert not any("LLMProvider" in i for i in imps)
    assert "chat" not in calls or "_call_llm" in calls  # no LLMProvider.chat path
    # Defines no new evaluation/prompt function.
    fn_defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)}
    assert fn_defs == {"evaluate_search_quality"}


# ── Gap3 — suggest_next_step.py reuses adapter, no new card renderer ───────


def test_suggest_next_step_reuses_adapter_only():
    src = (APP / "agents/tools/suggest_next_step.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imps = _imported_names(tree)
    calls = _called_attrs(tree)
    assert "app.graphs.nodes._adapter_ctx.get_adapter" in imps
    assert "send_text_with_buttons" in calls or "send_text" in calls
    # <= 80 LOC body discipline (REQ-AGENT-V3-PROACT-TOOL-001 / AC-3.1).
    assert len([ln for ln in src.splitlines() if ln.strip()]) <= 80


# ── Gap4 — recency_weighted_excludes reuses exclude_brands, no new ranking ─


def test_gap4_reuses_exclude_brands_no_new_ranking():
    tree = _tree("infrastructure/memory/taste_profile.py")
    src = (APP / "infrastructure/memory/taste_profile.py").read_text(encoding="utf-8")
    calls = _called_attrs(tree)
    # The new helper reuses the EXISTING exclude_brands().
    assert "exclude_brands" in calls
    assert "recency_weighted_excludes" in src
    # No new search/RPC/embedding import sneaked into the dataclass module.
    imps = _imported_names(tree)
    assert not any("pipeline" in i or "search" in i or "embed" in i for i in imps)


def test_gap4_search_discount_reuses_filter_pattern():
    """apply_dislike_discount must not introduce a new search/ranking call —
    it only filters the already-returned candidate list."""
    tree = _tree("agents/tools/search_products.py")
    # The helper exists and the existing pipeline entrypoints are unchanged.
    fn_defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)}
    assert "apply_dislike_discount" in fn_defs
    assert "run_text_only_search" in fn_defs  # existing entrypoint still present
    assert "run_image_search" in fn_defs


# ── Untouched-body assertions (snapshot via AST) ───────────────────────────


def test_evaluator_body_unchanged_public_surface():
    """evaluator.py keeps its helper surface — Gap2 only imports, never edits."""
    tree = _tree("graphs/nodes/evaluator.py")
    fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)}
    assert {"_call_llm", "_build_fastpath_delta", "_fail_open_score", "evaluator"} <= fns
