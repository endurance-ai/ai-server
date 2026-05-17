"""SPEC-AGENT-V2-REACT / T-004 — ReAct loop core engine.

ANCHOR for the agent path. Wraps the iteration loop with all safety guards:
- Iteration cap (REQ-AGENT-LOOP-ITERATION-001)
- Infinite-loop guard: 3 consecutive identical tool calls (REQ-AGENT-FAILURE-INFINITE-001)
- Token budget cap (REQ-AGENT-PERF-TURN-BUDGET-001)
- Per-LLM-call timeout (REQ-AGENT-FAILURE-LLM-JSON-001)
- Per-tool dispatch timeout (REQ-AGENT-FAILURE-TOOL-001)
- JSON malformation retry (1x) → exhaustion (REQ-AGENT-FAILURE-LLM-JSON-001)
- Args validation against TypedDict (REQ-AGENT-TOOL-DISPATCH-001)
- tool_call event emission per dispatch (REQ-AGENT-OBS-001)
- Fallback respond on exhaustion (REQ-AGENT-LOOP-EXHAUSTION-001)

@MX:ANCHOR: [AUTO] SPEC-AGENT-V2-REACT ReAct loop core — fan_in from graph node
@MX:REASON: All agent-path turns funnel through `run_react_loop`; safety guards
  live here so deprecation of v1 nodes (critique_apply/evaluator/respond) is safe.
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from importlib import import_module
from typing import Any

from langchain_core.messages import ToolMessage

from app.agents.llm_client import get_llm
from app.agents.tool_registry import REGISTRY, validate_args
from app.channels.lang import session_lang
from app.channels.taste_profile import user_key_for
from app.core.config import settings
from app.graphs.state import WorkingState
from app.observability.conversation_log import emit

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are kiko, a playful fashion-curator AI for kiko.ai. You operate as a ReAct agent: "
    "decide which tool to call at each step. ALWAYS end with the `respond` tool which sends "
    "the final natural-language reply to the user. `respond` takes ONLY a `text` argument — "
    "never pass cards or product data; the system attaches the search result cards "
    "automatically from the most recent search.\n\n"
    "Voice: bright, bouncy, like Puss-in-Boots. Korean input → reply in 해요체 Korean. "
    "English input → reply in lively English. Never mix languages in one reply.\n\n"
    "Tools available: analyze_image, search_products, refine_search, update_taste, "
    "ask_user_clarification, get_recent_history, respond. Use the minimum number of tool "
    "calls needed. Do NOT call the same tool with identical args 3 times in a row.\n\n"
    "NEVER provide an image_url argument to any tool, and never invent one. Imagery is "
    "resolved internally from session state — `search_products` works from `text_query` "
    "alone. For text requests, pass a concise ENGLISH `text_query` (e.g. 'leather "
    "loafers', 'trench coat').\n\n"
    "When the user message includes a `user_selected_item:` line (the user just tapped an "
    "item the photo analysis found), do NOT ask what they are looking for — immediately call "
    "`search_products` using the provided `suggested_query` as `text_query`, then `respond` "
    "with a short text reply (the product cards are attached automatically).\n\n"
    "Avoid redundant tool calls:\n"
    "- If a previous `search_products` or `refine_search` result in this conversation already "
    "returned candidates, your NEXT action MUST be `respond` with a short text reply (cards "
    "auto-attached). Do NOT call search again with the same query, and do NOT call "
    "`analyze_image`.\n"
    "- Do NOT call `analyze_image` if vision context is already present — i.e. the user "
    "message contains any of `detected_items:`, `user_selected_item:`, or `style_node:` "
    "(the photo was already analyzed before this loop). Only call `analyze_image` when the "
    "user sent a NEW image this turn AND no such vision context is present.\n"
    "- Prefer the fewest tool calls. Once you have enough to answer, call `respond`. Never "
    "repeat a tool with identical args."
)


# SPEC-AGENT-V3-REACT Gap3 — proactive directive, appended to the system
# prompt ONLY when AGENT_V3_PROACTIVE_ENABLED. flag OFF → not appended →
# _SYSTEM_PROMPT byte-identical V2 (REQ-AGENT-V3-PROACT-FLAG-001).
# @MX:SPEC: SPEC-AGENT-V3-REACT
_PROACTIVE_DIRECTIVE = (
    "Be proactive. When a `search_products` / `refine_search` result is weak "
    "(candidates_count < 3), do NOT just respond with an apology — first call "
    "`suggest_next_step` to offer concrete follow-up options (similar items, "
    "different fit, another mood, or broaden). When the user's intent is "
    "ambiguous, prefer calling `ask_user_clarification` BEFORE searching rather "
    "than guessing. Always end the turn with `respond`."
)


# Transient-error backoff schedules (seconds). Indexed by attempt number
# (0-based: the i-th retry sleeps _LLM_BACKOFF[i] before re-issuing). Kept
# short — the per-turn wall-clock / token budget still bounds total cost.
_LLM_BACKOFF = (0.6, 1.4)
_TOOL_BACKOFF = (0.8,)

_TRANSIENT_STATUS = {500, 502, 503, 504}
_TRANSIENT_MARKERS = (
    "internal server error",
    "throttl",
    "503",
    "502",
    "504",
    "timeout",
    "timed out",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def _is_transient(exc: BaseException) -> bool:
    """Classify whether `exc` is a retryable transient infra error.

    Conservative by design: when unsure, return False so real bugs (4xx,
    JSON-malformation, contract violations) are NOT masked by retries.

    Transient = HTTP 5xx (500/502/503/504) from httpx/openai/litellm, OR a
    timeout (asyncio.TimeoutError / builtin TimeoutError), OR an error message
    that clearly signals throttling / 5xx / timeout.
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True

    # Structured status code (httpx.HTTPStatusError, openai.APIStatusError,
    # litellm exceptions all expose .status_code or a nested .response).
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    if isinstance(status, int):
        return status in _TRANSIENT_STATUS

    # Fall back to message inspection — but only for clearly-transient markers.
    # 4xx (400/401/403/404/422) never match these markers, so they fail closed.
    msg = str(exc).lower()
    if "400" in msg or "401" in msg or "403" in msg or "404" in msg or "422" in msg:
        # A 4xx code present in the message → treat as NON-transient even if a
        # transient marker also appears (avoid masking a real client error).
        return False
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _resolve_dispatcher(tool_name: str):
    meta = REGISTRY[tool_name]
    module_path, fn_name = meta["dispatch_fn_path"].split(":", 1)
    module = import_module(module_path)
    return getattr(module, fn_name)


def _args_summary(args: dict[str, Any]) -> dict[str, Any]:
    """Compact summary safe for tool_call event payload."""
    out: dict[str, Any] = {}
    for k, v in list(args.items())[:8]:
        if isinstance(v, str):
            out[k] = v[:120]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list):
            out[k] = f"list[{len(v)}]"
        elif isinstance(v, dict):
            out[k] = f"dict[{len(v)}]"
        else:
            out[k] = str(type(v).__name__)
    return out


def _is_identical(a: dict[str, Any], b: dict[str, Any]) -> bool:
    try:
        return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return False


def _build_ctx(state: WorkingState, sess: Any) -> dict[str, Any]:
    """Tool dispatch context — passed alongside args to every tool."""
    return {
        "chat_id": state.chat_id,
        "from_user_id": state.from_user_id,
        "user_key": user_key_for(state.from_user_id, state.chat_id),
        "image_url": state.image_url,
        "thread_id": state.thread_id,
        "lang": session_lang(sess),
        "text_query": (state.message.text or "") if state.message else "",
        "style_node_primary": state.vision_outfit_style_node_primary,
    }


def _detected_items(state: WorkingState, sess: Any) -> list[dict[str, Any]]:
    """Best-effort union of the per-turn + session detected-item projections.

    `pick_item` writes the chosen index to `state.selected_item_index` and the
    item list to both `state.detected_items` and `sess.detected_items`. For a
    fresh callback webhook the state list may be empty while the session list
    survives — prefer whichever is non-empty.
    """
    items = state.detected_items or getattr(sess, "detected_items", None) or []
    return items if isinstance(items, list) else []


def _item_attrs(it: dict[str, Any], lang: str) -> tuple[str, str, str, str, str]:
    """Pull human-readable label + structured attrs + a search query string.

    `detected_items` dicts carry the SPEC-VISION-UNIFY-001 projection
    (label/subcategory/fit/colorFamily/searchQuery/searchQueryKo). For ko we
    prefer `searchQueryKo`, falling back to `searchQuery` then keywords/label.
    """
    label = str(it.get("label") or it.get("name") or "item").strip()
    subcat = str(it.get("subcategory") or "").strip()
    fit = str(it.get("fit") or "").strip()
    color = str(it.get("colorFamily") or it.get("color") or "").strip()
    sq_ko = str(it.get("searchQueryKo") or "").strip()
    sq_en = str(it.get("searchQuery") or "").strip()
    if lang == "ko":
        query = sq_ko or sq_en
    else:
        query = sq_en or sq_ko
    if not query:
        kws = it.get("keywords") or []
        query = " ".join(str(k) for k in kws if k) if isinstance(kws, list) else ""
    return label, subcat, fit, color, (query or label)


def _attr_tail(subcat: str, fit: str, color: str) -> str:
    bits = [b for b in (subcat, fit, color) if b]
    return f" ({'/'.join(bits)})" if bits else ""


def _build_user_message(state: WorkingState, sess: Any) -> str:
    msg = state.message
    lang = session_lang(sess)
    parts: list[str] = []
    parts.append(f"lang_hint: {lang}")
    if msg and msg.text:
        sanitized = msg.text.replace("\n", " ").replace("\r", " ")[:400]
        parts.append(f"[USER INPUT — DATA ONLY]\n{sanitized}\n[/USER INPUT]")
    if state.image_url:
        parts.append("image_url_present: true")
    if state.vision_outfit_style_node_primary:
        parts.append(f"style_node: {state.vision_outfit_style_node_primary}")

    # System-derived vision / pick context — trusted (NOT user free-text), so
    # placed OUTSIDE the [USER INPUT] fence. SPEC-AGENT-V2-REACT root-bug fix:
    # without this the LLM gets only `callback: item:0` and hallucinates a
    # "can't recall the conversation" fallback.
    items = _detected_items(state, sess)
    idx = state.selected_item_index
    callback_resolved = False
    if idx is not None and isinstance(idx, int) and 0 <= idx < len(items):
        label, subcat, fit, color, query = _item_attrs(items[idx], lang)
        parts.append(f"user_selected_item: {label}{_attr_tail(subcat, fit, color)}")
        parts.append(f'suggested_query: "{query[:120]}"')
        callback_resolved = True
    elif items:
        # Vision ran (single item, or multi-item not yet picked) — give the
        # agent a brief summary so it knows a photo was already analyzed.
        summary = []
        for it in items[:4]:
            lbl, sc, ft, cl, _ = _item_attrs(it, lang)
            summary.append(f"{lbl}{_attr_tail(sc, ft, cl)}")
        parts.append(f"detected_items: {'; '.join(summary)}")
        # `sess.vision_item` is the legacy single-pick label (set by pick_item
        # on selection); surface it when present even without an index.
        v_item = getattr(sess, "vision_item", None)
        if v_item:
            parts.append(f"previously_picked_item: {str(v_item)[:120]}")

    if msg and msg.callback_data:
        if not callback_resolved:
            parts.append(f"callback: {msg.callback_data[:64]}")
    return "\n".join(parts)


# SPEC-AGENT-V3-REACT Gap2 — per-turn Reflexion bound (REQ-AGENT-V3-REFLEX-BOUND-001).
_V3_REFLEXION_COUNT_KEY = "_v3_reflexion_count"


async def _maybe_reflexion(
    state: WorkingState, sess: Any, ctx: dict[str, Any], turn_deadline: float
) -> dict[str, Any] | None:
    """Run one bounded Reflexion evaluation. Returns the `_quality` dict to
    merge into the ToolMessage, or None when skipped (cap reached / deadline /
    error). NEVER mutates `history` or consumes a ReAct iteration.

    @MX:WARN: [AUTO] The evaluator call MUST be wrapped in
      asyncio.wait_for(timeout=remaining turn budget). Pre-check is
      insufficient — EVALUATOR_TIMEOUT_S (8s) may exceed the residual budget,
      so cancel-on-overrun is normative (REQ-AGENT-V3-REFLEX-DEADLINE-001).
    @MX:REASON: a non-cancelled slow evaluator overruns turn_deadline and the
      inherited p95<8s budget; the wait_for wrap mechanically bounds it.
    @MX:SPEC: SPEC-AGENT-V3-REACT
    """
    # Bound: per-turn evaluator-call cap = SELF_CRITIQUE_MAX_ITERATIONS.
    max_calls = max(0, int(settings.SELF_CRITIQUE_MAX_ITERATIONS))
    count = int(ctx.get(_V3_REFLEXION_COUNT_KEY, 0))
    if count >= max_calls:
        return None

    # Residual-budget timeout wrap (REQ-AGENT-V3-REFLEX-DEADLINE-001, D2). The
    # pre-check alone is insufficient; the wait_for cancels an overrunning
    # evaluator at the residual boundary (NOT at EVALUATOR_TIMEOUT_S).
    remaining = max(0.0, turn_deadline - time.monotonic())
    if remaining <= 0.0:
        return {"skipped": True, "reason": "deadline"}

    ctx[_V3_REFLEXION_COUNT_KEY] = count + 1
    try:
        from app.agents._reflexion import evaluate_search_quality

        quality = await asyncio.wait_for(evaluate_search_quality(state, sess, ctx), timeout=remaining)
        return quality
    except TimeoutError:
        logger.warning("[agent_v3] reflexion cancelled at residual budget boundary")
        return {"skipped": True, "reason": "deadline"}
    except Exception as exc:  # noqa: BLE001 — fail-open: never break the loop
        logger.warning("[agent_v3] reflexion raised, fail-open: %r", exc)
        return None


async def _fallback_respond(state: WorkingState, sess: Any, reason: str) -> dict[str, Any]:
    """REQ-AGENT-LOOP-EXHAUSTION-001 — graceful fallback."""
    lang = session_lang(sess)
    if lang == "ko":
        text = "잠깐만요, 생각이 좀 꼬였어요 🙈 다시 한 번 말씀해 주실래요?"
    else:
        text = "Sorry, I got a little tangled up 🙈 Could you try that again?"
    try:
        from app.agents.tools.respond import dispatch as respond_dispatch

        await respond_dispatch({"text": text}, _build_ctx(state, sess))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[agent_v2] fallback respond failed: %r", exc)
    logger.info("[agent_v2] exhausted: %s", reason)
    return {"response_text": text, "exhausted_reason": reason}


async def run_react_loop(state: WorkingState, sess: Any) -> dict[str, Any]:
    """Run the ReAct loop. Returns a state delta dict for the LangGraph node."""
    llm = get_llm()
    if llm is None:
        fb = await _fallback_respond(state, sess, "llm_unavailable")
        return {
            "agent_iterations": 0,
            "agent_status": "exhausted",
            "tool_call_history": [],
            "response_text": fb.get("response_text"),
        }

    max_iter = max(1, int(settings.AGENT_MAX_ITERATIONS))
    token_budget = max(0, int(settings.AGENT_TURN_TOKEN_BUDGET))
    tool_timeout = float(settings.AGENT_TOOL_TIMEOUT_S)
    llm_timeout = float(settings.AGENT_LLM_TIMEOUT_S)
    llm_max_retries = max(0, int(settings.AGENT_LLM_MAX_RETRIES))
    tool_max_retries = max(0, int(settings.AGENT_TOOL_MAX_RETRIES))

    # Hard wall-clock ceiling for the whole turn so retries can never push us
    # past the existing exhaust budget. Derived from the per-call timeouts ×
    # iteration cap (the same envelope the loop already implicitly had); the
    # retry layer is bounded WITHIN this, not added on top.
    turn_t0 = time.monotonic()
    turn_deadline = turn_t0 + (max_iter * (llm_timeout + tool_timeout)) + sum(_LLM_BACKOFF) + sum(_TOOL_BACKOFF)

    ctx = _build_ctx(state, sess)
    user_key = ctx["user_key"]

    # SPEC-AGENT-V3-REACT Gap1 — flag-gated system-context assembly. With the
    # flag OFF the system content is byte-identical V2 (`_SYSTEM_PROMPT`); the
    # `_memory_context` module is not even imported. With it ON, a
    # system-derived, char-capped memory block is appended (the
    # `_build_user_message` [USER INPUT — DATA ONLY] fence is UNCHANGED).
    # @MX:SPEC: SPEC-AGENT-V3-REACT
    # Assembly order is _SYSTEM_PROMPT [+_PROACTIVE_DIRECTIVE] [+memory_context]
    # (REQ-AGENT-V3-PROACT-PROMPT-001 / E3). Each segment is independently
    # flag-gated; all flags OFF → byte-identical V2 `_SYSTEM_PROMPT`.
    system_content = _SYSTEM_PROMPT
    if settings.AGENT_V3_PROACTIVE_ENABLED:
        system_content = f"{system_content}\n\n{_PROACTIVE_DIRECTIVE}"
        logger.info("💡 [v3:proactive] suggest_next_step offered")
    else:
        logger.info("💡 [v3:proactive] skip · flag off")
    if settings.AGENT_V3_MEMORY_INJECTION_ENABLED:
        try:
            from app.agents._memory_context import build_memory_context

            mem_block = await build_memory_context(
                state, sess, ctx, max_tokens=int(settings.AGENT_V3_MEMORY_MAX_TOKENS)
            )
            system_content = f"{system_content}\n\n{mem_block}"
            if "(no taste history yet)" in mem_block:
                logger.info("🧠 [v3:memory] skip · empty (get_recent_history 0 events)")
            else:
                logger.info("🧠 [v3:memory] injected · ~chars=%d", len(mem_block))
        except Exception as exc:  # noqa: BLE001 — fail-soft to current system content
            logger.warning("[agent_v3] memory injection failed, falling back: %r", exc)
            logger.info("🧠 [v3:memory] skip · build error")
    else:
        logger.info("🧠 [v3:memory] skip · flag off")

    # Use plain dicts to construct messages — avoids langchain message-class imports.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": _build_user_message(state, sess)},
    ]

    history: list[dict[str, Any]] = []
    cumulative_tokens = 0
    iterations = 0
    json_malform_streak = 0
    status: str = "running"

    for it in range(1, max_iter + 1):
        iterations = it
        logger.info("🔄 [agent] iter %d/%d", it, max_iter)

        # Token budget guard (REQ-AGENT-PERF-TURN-BUDGET-001).
        if token_budget and cumulative_tokens >= token_budget:
            fb = await _fallback_respond(state, sess, "token_budget_exceeded")
            return {
                "agent_iterations": iterations,
                "agent_status": "exhausted",
                "tool_call_history": history,
                "response_text": fb.get("response_text"),
            }

        # LLM call with timeout + transient-error retry (inner resilience
        # layer — a retry does NOT consume a ReAct iteration). On a transient
        # infra error (5xx / throttle / timeout) we re-issue the SAME logical
        # step up to `llm_max_retries` times with short backoff. Non-transient
        # errors fall through to the existing exhaustion fallback unchanged.
        ai_msg = None
        last_exc: BaseException | None = None
        last_reason = "llm_timeout"
        for attempt in range(llm_max_retries + 1):
            try:
                ai_msg = await asyncio.wait_for(llm.ainvoke(messages), timeout=llm_timeout)
                break
            except (TimeoutError, Exception) as exc:  # noqa: BLE001
                last_exc = exc
                last_reason = (
                    "llm_timeout"
                    if isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                    else f"llm_error:{type(exc).__name__}"
                )
                transient = _is_transient(exc)
                retries_left = attempt < llm_max_retries
                if not (transient and retries_left):
                    break
                backoff = _LLM_BACKOFF[min(attempt, len(_LLM_BACKOFF) - 1)]
                # Respect the per-turn wall-clock ceiling — never retry past it.
                if time.monotonic() + backoff >= turn_deadline:
                    logger.warning(
                        "[agent_v2] LLM transient error but turn deadline reached, no more retries: %r",
                        exc,
                    )
                    break
                logger.warning(
                    "[agent_v2] LLM transient error (attempt %d/%d), retrying in %.1fs: %r",
                    attempt + 1,
                    llm_max_retries,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)

        if ai_msg is None:
            logger.warning("[agent_v2] LLM raised (retries exhausted): %r", last_exc)
            fb = await _fallback_respond(state, sess, last_reason)
            return {
                "agent_iterations": iterations,
                "agent_status": "exhausted",
                "tool_call_history": history,
                "response_text": fb.get("response_text"),
            }

        # Approx token accounting — best-effort from usage_metadata.
        try:
            um = getattr(ai_msg, "usage_metadata", None) or {}
            cumulative_tokens += int(um.get("total_tokens", 0) or 0)
        except Exception:  # noqa: BLE001
            pass

        tool_calls = list(getattr(ai_msg, "tool_calls", None) or [])
        if not tool_calls:
            # No tool call — treat as JSON malformation (the LLM should always
            # call a tool, terminating with `respond`).
            json_malform_streak += 1
            if json_malform_streak >= 2:
                fb = await _fallback_respond(state, sess, "json_malform_repeated")
                return {
                    "agent_iterations": iterations,
                    "agent_status": "exhausted",
                    "tool_call_history": history,
                    "response_text": fb.get("response_text"),
                }
            # Corrective retry — append the assistant turn first, then a
            # user-role msg nudging tool use. The assistant turn must precede
            # any follow-up so the next ainvoke has a valid transcript.
            messages.append(ai_msg)
            messages.append(
                {
                    "role": "user",
                    "content": "Your previous response had no tool call. You MUST call a tool. "
                    "If you have nothing else to do, call `respond` with the final text.",
                }
            )
            continue
        json_malform_streak = 0

        # We honor only the first tool call per iteration (sequential ReAct).
        tc = tool_calls[0]
        tool_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
        raw_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
        if not isinstance(raw_args, dict):
            raw_args = {}
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if not tc_id:
            # Upstream contract violation: a tool_call without an id cannot be
            # answered with a matching ToolMessage (Bedrock Nova matches
            # toolUse/toolResult by id). Treat as a malformed-LLM event rather
            # than inventing an id (which reproduces the Bedrock 400).
            json_malform_streak += 1
            if json_malform_streak >= 2:
                fb = await _fallback_respond(state, sess, "json_malform_repeated")
                return {
                    "agent_iterations": iterations,
                    "agent_status": "exhausted",
                    "tool_call_history": history,
                    "response_text": fb.get("response_text"),
                }
            messages.append(ai_msg)
            messages.append(
                {
                    "role": "user",
                    "content": "Your previous tool call was missing an id. You MUST call a tool "
                    "with a valid id. If you have nothing else to do, call `respond`.",
                }
            )
            continue

        # Args validation.
        ok, err = validate_args(tool_name or "", raw_args)
        if not ok:
            history_entry = {
                "iter": it,
                "tool_name": tool_name,
                "args": _args_summary(raw_args),
                # P0-1: include args_full so the 3-consecutive-identical-call
                # infinite-loop guard can deep-compare repeated invalid-arg
                # calls too (stripped before persist on every return path).
                "args_full": raw_args,
                "error": f"invalid_args:{err}",
                "latency_ms": 0,
            }
            history.append(history_entry)
            # Append the assistant tool_use turn BEFORE its matching tool
            # result — Bedrock Nova (via LiteLLM) requires the toolUse block to
            # precede its toolResult block, matched by id.
            messages.append(ai_msg)
            messages.append(ToolMessage(content=json.dumps({"error": err}), tool_call_id=tc_id))
            # Emit tool_call (with error).
            emit(
                event_type="tool_call",
                user_key=user_key,
                chat_id=state.chat_id,
                thread_id=state.thread_id,
                turn_no=state.turn_no or 1,
                payload={
                    "tool_name": tool_name or "<unknown>",
                    "iteration_no": it,
                    "latency_ms": 0,
                    "error": err,
                    "args_summary": _args_summary(raw_args),
                },
            )
            continue

        # Infinite-loop guard — 3 consecutive identical (tool_name, args).
        if len(history) >= 2:
            last2 = history[-2:]
            if all(h.get("tool_name") == tool_name for h in last2) and all(
                _is_identical(h.get("args_full", {}), raw_args) for h in last2
            ):
                fb = await _fallback_respond(state, sess, "infinite_loop_guard")
                # Strip args_full before persisting history (keep small).
                for h in history:
                    h.pop("args_full", None)
                return {
                    "agent_iterations": iterations,
                    "agent_status": "exhausted",
                    "tool_call_history": history,
                    "response_text": fb.get("response_text"),
                }

        # Dispatch — with transient-error retry (tools internally call the
        # same throttled proxy: analyze_image / search both hit Bedrock via
        # LiteLLM). On a transient 5xx/timeout we retry the dispatch up to
        # `tool_max_retries` time(s) with short backoff before recording the
        # tool error and continuing the loop. Non-transient → record now.
        #
        # EXCEPTION: the TERMINAL `respond` tool is NEVER retried. Its side
        # effects (Telegram text + card carousel sends) are not idempotent —
        # a partial send followed by a full dispatch retry double-sends the
        # text and duplicates every card the user already saw (the real
        # SPEC-AGENT-V2-REACT bug). A retry has no benefit here (the user
        # already saw the message) and only harm; per-message idempotency
        # tracking would be strictly more complex for zero upside. So for
        # `respond`: max retries = 0 on ANY exception (transient or not), and
        # it gets a dedicated generous wall timeout (AGENT_RESPOND_TIMEOUT_S)
        # so a slow-but-legit 12-card carousel does not raise a false-positive
        # TimeoutError mid-send. `respond` terminates the loop regardless.
        is_terminal = bool(REGISTRY[tool_name]["terminates_loop"])
        effective_max_retries = 0 if is_terminal else tool_max_retries
        effective_timeout = float(settings.AGENT_RESPOND_TIMEOUT_S) if is_terminal else tool_timeout
        t0 = time.monotonic()
        result: dict[str, Any]
        dispatch_err: str | None = None
        for attempt in range(effective_max_retries + 1):
            try:
                dispatcher = _resolve_dispatcher(tool_name)
                result = await asyncio.wait_for(dispatcher(raw_args, ctx), timeout=effective_timeout)
                dispatch_err = None
                break
            except (TimeoutError, Exception) as exc:  # noqa: BLE001
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    result = {"ok": False, "error": "tool_timeout"}
                    dispatch_err = "tool_timeout"
                else:
                    logger.warning("[agent_v2] tool %s raised: %r", tool_name, exc)
                    result = {"ok": False, "error": f"exception:{type(exc).__name__}"}
                    dispatch_err = result["error"]
                transient = _is_transient(exc)
                retries_left = attempt < effective_max_retries
                if not (transient and retries_left):
                    break
                backoff = _TOOL_BACKOFF[min(attempt, len(_TOOL_BACKOFF) - 1)]
                if time.monotonic() + backoff >= turn_deadline:
                    logger.warning(
                        "[agent_v2] tool %s transient error but turn deadline reached: %r",
                        tool_name,
                        exc,
                    )
                    break
                logger.warning(
                    "[agent_v2] tool %s transient error (attempt %d/%d), retrying in %.1fs: %r",
                    tool_name,
                    attempt + 1,
                    tool_max_retries,
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if dispatch_err:
            logger.info("🔧 [tool:%s] → err %s %dms", tool_name, dispatch_err, latency_ms)
        else:
            logger.info("🔧 [tool:%s] → ok %dms", tool_name, latency_ms)

        history_entry = {
            "iter": it,
            "tool_name": tool_name,
            "args": _args_summary(raw_args),
            "args_full": raw_args,
            "result_summary": {
                k: result.get(k) for k in ("ok", "error", "candidates_count", "card_sent") if k in result
            },
            "latency_ms": latency_ms,
            "error": dispatch_err,
        }
        history.append(history_entry)

        emit(
            event_type="tool_call",
            user_key=user_key,
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=state.turn_no or 1,
            payload={
                "tool_name": tool_name,
                "iteration_no": it,
                "latency_ms": latency_ms,
                "error": dispatch_err,
                "args_summary": _args_summary(raw_args),
                "result_summary": history_entry["result_summary"],
            },
        )

        # ── SPEC-AGENT-V3-REACT Gap2 — Reflexion in-loop evaluation ────────
        # flag ON & tool ∈ {search_products, refine_search} & result.ok →
        # wrap the existing evaluator helper, merge a `_quality` marker into
        # the ToolMessage the LLM sees next so it can AUTONOMOUSLY decide
        # refine vs respond (V3 never forces a refine).
        #
        # Invariants (REQ-AGENT-V3-REFLEX-BOUND-001):
        #  - per-turn ctx counter `_v3_reflexion_count` < SELF_CRITIQUE_MAX_ITERATIONS
        #  - evaluator call does NOT touch `history` / `tool_call_history`
        #  - evaluator call does NOT consume a ReAct iteration (in-dispatch
        #    side call) → infinite-loop guard unaffected
        # flag OFF → `result` unchanged → ToolMessage byte-identical V2.
        _reflexion_eligible = (
            tool_name in ("search_products", "refine_search") and isinstance(result, dict) and result.get("ok")
        )
        if _reflexion_eligible and settings.AGENT_V3_REFLEXION_ENABLED:
            quality = await _maybe_reflexion(state, sess, ctx, turn_deadline)
            if quality is not None:
                result = {**result, "_quality": quality}
                if quality.get("skipped") and quality.get("reason") == "deadline":
                    logger.info("🔬 [v3:reflexion] skip · deadline (residual≤0)")
                else:
                    _score = quality.get("score")
                    _decision = "refine" if quality.get("retry_suggested") else "accept"
                    logger.info("🔬 [v3:reflexion] eval score=%s → %s", _score, _decision)
            else:
                logger.info("🔬 [v3:reflexion] skip · cap/error")
        elif _reflexion_eligible:
            logger.info("🔬 [v3:reflexion] skip · flag off")

        # Termination check (REQ-AGENT-LOOP-TERMINATION-001).
        if REGISTRY[tool_name]["terminates_loop"]:
            status = "done"
            break

        # Append the assistant tool_use turn BEFORE its matching tool result
        # for the next LLM turn — Bedrock Nova (via LiteLLM) requires the
        # toolUse block to precede its toolResult block, matched by id.
        messages.append(ai_msg)
        messages.append(ToolMessage(content=json.dumps(result, default=str)[:2000], tool_call_id=tc_id))

    else:
        # for-else: loop completed without break (no respond) → exhaustion.
        fb = await _fallback_respond(state, sess, "iteration_cap_reached")
        # Strip args_full before persisting history (keep small).
        for h in history:
            h.pop("args_full", None)
        return {
            "agent_iterations": iterations,
            "agent_status": "exhausted",
            "tool_call_history": history,
            "response_text": fb.get("response_text"),
        }

    # Strip args_full before persisting.
    for h in history:
        h.pop("args_full", None)
    logger.info("🏁 [agent] respond · iters=%d tokens≈%d", iterations, cumulative_tokens)
    return {
        "agent_iterations": iterations,
        "agent_status": status,
        "tool_call_history": history,
        "response_text": None,
    }
