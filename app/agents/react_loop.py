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
    "the final natural-language reply (and optional product cards) to the user.\n\n"
    "Voice: bright, bouncy, like Puss-in-Boots. Korean input → reply in 해요체 Korean. "
    "English input → reply in lively English. Never mix languages in one reply.\n\n"
    "Tools available: analyze_image, search_products, refine_search, update_taste, "
    "ask_user_clarification, get_recent_history, respond. Use the minimum number of tool "
    "calls needed. Do NOT call the same tool with identical args 3 times in a row.\n\n"
    "When the user message includes a `user_selected_item:` line (the user just tapped an "
    "item the photo analysis found), do NOT ask what they are looking for — immediately call "
    "`search_products` using the provided `suggested_query` as `text_query`, then `respond` "
    "with the product cards."
)


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

    ctx = _build_ctx(state, sess)
    user_key = ctx["user_key"]

    # Use plain dicts to construct messages — avoids langchain message-class imports.
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(state, sess)},
    ]

    history: list[dict[str, Any]] = []
    cumulative_tokens = 0
    iterations = 0
    json_malform_streak = 0
    status: str = "running"

    for it in range(1, max_iter + 1):
        iterations = it

        # Token budget guard (REQ-AGENT-PERF-TURN-BUDGET-001).
        if token_budget and cumulative_tokens >= token_budget:
            fb = await _fallback_respond(state, sess, "token_budget_exceeded")
            return {
                "agent_iterations": iterations,
                "agent_status": "exhausted",
                "tool_call_history": history,
                "response_text": fb.get("response_text"),
            }

        # LLM call with timeout.
        try:
            ai_msg = await asyncio.wait_for(llm.ainvoke(messages), timeout=llm_timeout)
        except TimeoutError:
            fb = await _fallback_respond(state, sess, "llm_timeout")
            return {
                "agent_iterations": iterations,
                "agent_status": "exhausted",
                "tool_call_history": history,
                "response_text": fb.get("response_text"),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[agent_v2] LLM raised: %r", exc)
            fb = await _fallback_respond(state, sess, f"llm_error:{type(exc).__name__}")
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

        # Dispatch.
        t0 = time.monotonic()
        result: dict[str, Any]
        dispatch_err: str | None = None
        try:
            dispatcher = _resolve_dispatcher(tool_name)
            result = await asyncio.wait_for(dispatcher(raw_args, ctx), timeout=tool_timeout)
        except TimeoutError:
            result = {"ok": False, "error": "tool_timeout"}
            dispatch_err = "tool_timeout"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[agent_v2] tool %s raised: %r", tool_name, exc)
            result = {"ok": False, "error": f"exception:{type(exc).__name__}"}
            dispatch_err = result["error"]
        latency_ms = int((time.monotonic() - t0) * 1000)

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
    return {
        "agent_iterations": iterations,
        "agent_status": status,
        "tool_call_history": history,
        "response_text": None,
    }
