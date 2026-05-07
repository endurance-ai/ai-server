"""SPEC-AGENTIC-CRITIQUE-001 — `evaluator` node (Reflexion-style self-critique).

# @MX:ANCHOR: [AUTO] Evaluator node — entry point for self-critique loop.
# @MX:REASON: fan_in >= 3 (search_node, retry path from itself, future hooks).
# @MX:SPEC: SPEC-AGENTIC-CRITIQUE-001 / REQ-CRITIQUE-EVAL-001..003

Reads candidates + vision context, asks an LLM whether the search results
actually match the user's intent. Emits a `CritiqueScore` that the routing
layer (`_route_after_evaluator`) consumes to decide retry vs ship.

Empty-result fast-path (REQ-CRITIQUE-EMPTY-001) skips the LLM call entirely
on the FIRST iteration when candidates are empty — synthesizes a broaden delta
from the configured `SELF_CRITIQUE_FASTPATH_DROP_FILTERS`. The most common
failure mode (RPC raw_count=0) does not pay the evaluator-LLM tax.

Failure paths (REQ-CRITIQUE-EVAL-003): any LLM/timeout/parse/validation error
returns the synthetic fail-open score `(score=1.0, retry=False)` so the user
gets the original results; the failure is logged at WARNING.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx
from pydantic import ValidationError

from app.channels.session import get_store
from app.core.config import settings
from app.graphs.nodes._evaluator_models import CritiqueDelta, CritiqueScore
from app.graphs.nodes._evaluator_prompt import SYSTEM_PROMPT, build_user_prompt
from app.graphs.state import WorkingState
from app.observability.langfuse import observe
from app.providers.llm import LLMProvider

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _summarize_delta(delta: CritiqueDelta | None) -> str | None:
    """Compact one-line repr for Langfuse + log line."""
    if delta is None:
        return None
    parts: list[str] = [delta.intent]
    if delta.drop_min_price:
        parts.append("drop_min_price")
    if delta.drop_max_price:
        parts.append("drop_max_price")
    if delta.drop_filters:
        parts.append("drop:" + ",".join(delta.drop_filters[:3]))
    if delta.exclude_brands:
        parts.append("xb:" + ",".join(delta.exclude_brands[:2]))
    if delta.exclude_keywords:
        parts.append("xk:" + ",".join(delta.exclude_keywords[:2]))
    if delta.boost_keywords:
        parts.append("bk:" + ",".join(delta.boost_keywords[:2]))
    if delta.color_override:
        parts.append(f"color={delta.color_override}")
    if delta.fit_override:
        parts.append(f"fit={delta.fit_override}")
    return ":".join(parts)


def _build_fastpath_delta() -> CritiqueDelta:
    """REQ-CRITIQUE-EMPTY-001 — LLM-free broaden delta on empty results."""
    drops = settings.self_critique_fastpath_drop_filters
    return CritiqueDelta(
        intent="broaden",
        drop_min_price="min_price" in drops,
        drop_max_price="max_price" in drops,
        drop_filters=[d for d in drops if d not in {"min_price", "max_price"}],
    )


def _fail_open_score(reason: str) -> CritiqueScore:
    """REQ-CRITIQUE-EVAL-003 — synthetic fail-open: ship original results."""
    return CritiqueScore(
        score=1.0,
        reasoning=f"evaluator failed — {reason}"[:200],
        suggested_delta=None,
        retry=False,
    )


async def _call_llm(prompt_user: str) -> CritiqueScore:
    """Issue the LiteLLM call + JSON-parse + Pydantic-validate. Fail-open on
    any error (REQ-CRITIQUE-EVAL-003)."""
    timeout_s = max(0.1, settings.EVALUATOR_TIMEOUT_S)
    try:
        resp = await asyncio.wait_for(
            LLMProvider.chat(
                model=settings.EVALUATOR_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt_user},
                ],
                temperature=settings.EVALUATOR_TEMPERATURE,
                max_tokens=settings.EVALUATOR_MAX_TOKENS,
                response_format={"type": "json_object"},
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        logger.warning("[evaluator] LLM timeout — fail-open")
        return _fail_open_score("timeout")
    except httpx.HTTPError as exc:
        logger.warning("[evaluator] LLM HTTP error %s — fail-open", type(exc).__name__)
        return _fail_open_score(f"http:{type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[evaluator] LLM unknown error %s — fail-open", type(exc).__name__)
        return _fail_open_score(f"unknown:{type(exc).__name__}")

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return _fail_open_score("no_content")
    if not isinstance(content, str) or not content.strip():
        return _fail_open_score("empty_content")

    parsed: Any | None = None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(content)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        return _fail_open_score("parse_error")

    try:
        return CritiqueScore.model_validate(parsed)
    except ValidationError as exc:
        # @MX:NOTE: [AUTO] Validation failure logs the unknown field for prompt tuning.
        logger.warning("[evaluator] schema validation failed: %s — fail-open", exc.errors()[:2])
        return _fail_open_score("validation_error")


@observe(name="evaluator")
async def evaluator(state: WorkingState) -> dict:
    """REQ-CRITIQUE-EVAL-001 — emit a CritiqueScore + state delta.

    The routing layer (`_route_after_evaluator`) reads `critique_pending_delta`
    and `critique_retry_count` to decide retry-vs-ship. The evaluator itself
    is stateless on its inputs — it only mutates the documented WorkingState
    fields.
    """
    started_ms = time.monotonic() * 1000.0
    iteration = state.critique_retry_count  # 0 on first eval, increments before re-entry
    candidates = list(state.candidates or [])
    candidates_in = len(candidates)

    sess = get_store().get_or_create(state.chat_id)
    vision_item = state.vision_selected_item or sess.vision_item

    # ── Empty-result fast-path (REQ-CRITIQUE-EMPTY-001) ──────────────────
    use_fastpath = candidates_in == 0 and iteration == 0
    score: CritiqueScore
    source: str
    if use_fastpath:
        delta = _build_fastpath_delta()
        score = CritiqueScore(
            score=0.0,
            reasoning="empty results — fast-path broaden",
            suggested_delta=delta,
            retry=True,
        )
        source = "fast_path"
        logger.info(
            "[CRITIQUE][fast-path] dropping=%s iteration=%d",
            ",".join(settings.self_critique_fastpath_drop_filters),
            iteration,
        )
    else:
        # LLM path
        user_prompt = build_user_prompt(
            vision_item=vision_item,
            user_intent=sess.user_intent,
            candidates=candidates,
        )
        score = await _call_llm(user_prompt)
        source = "llm"

    elapsed_ms = int(time.monotonic() * 1000.0 - started_ms)
    delta_summary = _summarize_delta(score.suggested_delta)
    max_iter = settings.SELF_CRITIQUE_MAX_ITERATIONS
    n_label = iteration + 1
    n_total = max_iter + 1

    # REQ-CRITIQUE-OBSV-002 — structured CRITIQUE log line.
    if source == "llm":
        logger.info(
            "[CRITIQUE][%d/%d] score=%.2f retry=%s source=%s reason=%r "
            "candidates_in=%d candidates_out=%d elapsed_ms=%d",
            n_label,
            n_total,
            score.score,
            "true" if score.retry else "false",
            source,
            (score.reasoning or "")[:120],
            candidates_in,
            candidates_in,
            elapsed_ms,
        )

    trail_entry: dict[str, Any] = {
        "iteration": iteration,
        "score": score.score,
        "reasoning": (score.reasoning or "")[:200],
        "suggested_delta_summary": delta_summary,
        "candidates_count_in": candidates_in,
        "candidates_count_out": candidates_in,
        "source": source,
        "elapsed_ms": elapsed_ms,
    }

    # Capture wall-clock start on FIRST evaluator entry (REQ-CRITIQUE-LOOP-SAFETY-003).
    delta_dict: dict[str, Any] = {
        "critique_trail": [trail_entry],
        # We DO NOT increment retry_count here — that is bumped by the
        # apply-delta passthrough so routing stays pure (REQ-CRITIQUE-RETRY-001).
        "critique_pending_delta": score.suggested_delta if score.retry else None,
        "log_events": [f"evaluator: score={score.score:.2f} retry={score.retry} source={source}"],
    }
    if state.critique_started_at_ms is None:
        delta_dict["critique_started_at_ms"] = started_ms
    # Snapshot previous (current) iteration's score+candidates BEFORE retry overwrites.
    delta_dict["critique_previous_candidates"] = candidates
    delta_dict["critique_previous_score"] = score.score

    # REQ-CRITIQUE-RETRY-003 — exhaustion detection.
    #
    # Exhausted is true when the loop is going to exit on the EXIT branch with
    # a sub-threshold score AND the loop actually tried (i.e., this is not the
    # vanilla single-shot success case). We compute this here because routing
    # functions are pure and cannot mutate state.
    threshold = settings.SELF_CRITIQUE_THRESHOLD
    will_exit = not score.retry or iteration >= max_iter or score.suggested_delta is None
    # Stagnation / score-regression detection (mirror routing logic).
    if not will_exit and len(state.critique_trail or []) >= 1:
        prev = (state.critique_trail or [])[-1]
        prev_summary = prev.get("suggested_delta_summary")
        prev_score = prev.get("score")
        if prev_summary is not None and prev_summary == delta_summary:
            will_exit = True
        if isinstance(prev_score, (int, float)) and isinstance(score.score, float) and score.score < prev_score:
            will_exit = True
    # Wall-clock timeout guard.
    if not will_exit and state.critique_started_at_ms is not None:
        elapsed_s = (time.monotonic() * 1000.0 - state.critique_started_at_ms) / 1000.0
        if elapsed_s >= settings.SELF_CRITIQUE_TIMEOUT_S:
            will_exit = True

    last_retried = iteration > 0 or source == "fast_path"
    if will_exit and score.score < threshold and last_retried:
        delta_dict["critique_exhausted"] = True

    return delta_dict
