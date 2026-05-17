"""SPEC-AGENT-V3-REACT / Gap2 — Reflexion search-quality wrapper.

Resolves SPEC-AGENT-V2-REACT OQ-7 as an *in-loop helper call* (the β-variant —
NOT a graph node). Wraps the existing self-critique evaluator helpers; defines
NO new LLM prompt, NO new evaluation algorithm.

@MX:NOTE: [AUTO] WRAP-ONLY — composes existing helpers, no new evaluation logic:
  - app.graphs.nodes.evaluator._call_llm (the only LLM path; never
    LLMProvider.chat directly)
  - app.graphs.nodes.evaluator._build_fastpath_delta (empty-result LLM-free
    broaden)
  - app.graphs.nodes._evaluator_prompt.build_user_prompt (prompt builder)
  - app.graphs.nodes._evaluator_models.CritiqueScore (output contract)
@MX:SPEC: SPEC-AGENT-V3-REACT

CROSS-SPEC LIVE DEPENDENCY (RC8): this module promotes the evaluator helper
module AND the `SELF_CRITIQUE_*` + `EVALUATOR_*` env families to a LIVE
runtime dependency. SPEC-AGENT-V2-CLEANUP-001 (which lists `evaluator.py` and
those env families as deprecation/removal targets) MUST preserve all three:
(1) the evaluator eval-helper module (`_call_llm`/`_build_fastpath_delta`,
`_evaluator_prompt`, `_evaluator_models.CritiqueScore`) — only the V1 graph
node wiring may be removed; (2) the full `SELF_CRITIQUE_*` env family
(MAX_ITERATIONS/THRESHOLD/TIMEOUT_S); (3) the full `EVALUATOR_*` env family
(MODEL/MAX_TOKENS/TEMPERATURE/TIMEOUT_S, referenced by `_call_llm`). This is a
tracked cross-SPEC followup, not actionable here.

fail-open propagation: `evaluator._call_llm` already returns the synthetic
`CritiqueScore(score=1.0, retry=False)` on any error/timeout. We propagate
that verbatim (score=1.0 → no retry) — proving no reimplementation.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def evaluate_search_quality(state: Any, sess: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the last search via the existing evaluator helpers.

    Input candidates = `sess.last_results` full set (OQ-V3-1 resolved).
    vision/intent context use the same sources the V1 evaluator node uses.

    Returns a small dict derived from `CritiqueScore`:
      {"score": float, "retry_suggested": bool, "reason": str}
    On empty results the LLM-free fast-path broaden delta is used (no LLM call).
    """
    candidates = list(getattr(sess, "last_results", None) or [])
    user_intent = getattr(sess, "user_intent", None)
    # Mirror evaluator node's vision_item source exactly.
    vision_item = getattr(state, "vision_selected_item", None) or getattr(sess, "vision_item", None)

    try:
        from app.graphs.nodes._evaluator_prompt import build_user_prompt
        from app.graphs.nodes.evaluator import _build_fastpath_delta, _call_llm
    except Exception as exc:  # noqa: BLE001 — fail-open if helpers unavailable
        logger.warning("[agent_v3] reflexion helper import failed, fail-open: %r", exc)
        return {"score": 1.0, "retry_suggested": False, "reason": "evaluator_unavailable"}

    if not candidates:
        # Empty-result LLM-free broaden — reuse evaluator's fast-path delta.
        delta = _build_fastpath_delta()
        return {
            "score": 0.0,
            "retry_suggested": True,
            "reason": f"empty results — fast-path broaden ({delta.intent})",
        }

    prompt = build_user_prompt(
        vision_item=vision_item,
        user_intent=user_intent,
        candidates=candidates,
    )
    score = await _call_llm(prompt)  # async; fail-open inside on any error
    return {
        "score": float(score.score),
        "retry_suggested": bool(score.retry),
        "reason": (score.reasoning or "")[:200],
    }
