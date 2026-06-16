"""SPEC-AGENT-V2-REACT / REQ-AGENT-TOOL-CATALOG-001 — 7-tool registry.

Single source of truth for the LLM-callable tools. Each entry:
- `args` TypedDict — the schema the LLM must satisfy
- `result` TypedDict — the shape returned to the loop (LLM-consumable summary)
- `dispatch_fn_path` — dotted import path to the async dispatcher (lazy import)
- `description` — human-readable / LLM-readable doc string
- `langfuse_span_tag` — span name for observability
- `side_effect_doc` — operator-facing note on side effects

Schema enforcement: `validate_args(name, args)` runs at dispatch time
(REQ-AGENT-TOOL-DISPATCH-001) using TypedDict's `__required_keys__` /
`__optional_keys__` introspection.

@MX:NOTE: [AUTO] SPEC-AGENT-V2-REACT — 7-tool dispatch table single source of truth
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

__all__ = [
    "REGISTRY",
    "ToolMetadata",
    "ToolResult",
    # Tool args
    "AnalyzeImageArgs",
    "SearchProductsArgs",
    "RefineSearchArgs",
    "UpdateTasteArgs",
    "AskUserClarificationArgs",
    "GetRecentHistoryArgs",
    "RespondArgs",
    # SPEC-AGENT-V3-REACT Gap3 — 8th tool (flag-gated registration)
    "SuggestNextStepArgs",
    # Tool results
    "AnalyzeImageResult",
    "SearchProductsResult",
    "RefineSearchResult",
    "UpdateTasteResult",
    "AskUserClarificationResult",
    "GetRecentHistoryResult",
    "RespondResult",
    "SuggestNextStepResult",
    # Helpers
    "validate_args",
    "TOOL_NAMES",
]


# ── Common shapes ──────────────────────────────────────────────────────────


class ToolResult(TypedDict, total=False):
    """Generic result envelope. Per-tool TypedDicts narrow this."""

    ok: bool
    error: str | None
    result_summary: dict[str, Any]


# ── Args TypedDicts (7 tools) ──────────────────────────────────────────────


class AnalyzeImageArgs(TypedDict, total=False):
    # P1-2/4 (SPEC-AGENT-V2-REACT review): `image_url` is intentionally NOT a
    # field. Mirrors the SearchProductsArgs hardening — the tool sources the
    # resolved pin/og:image URL ONLY from ctx (populated pre-agent by the
    # resolve_image node). Exposing it let a prompt-injected LLM fabricate a
    # crafted SSRF URL; with no field, validate_args auto-rejects any supplied
    # `image_url` as unknown_keys.
    pass


class SearchProductsArgs(TypedDict, total=False):
    # NOTE (SPEC-AGENT-V2-REACT root-bug fix): `image_url` is intentionally
    # NOT a field here. The tool sources imagery internally from the resolved
    # session/ctx state. Exposing it let the LLM fabricate placeholder URLs
    # that Modal embedded → vector search missed the whole catalog → 0 results.
    text_query: str
    style_node_primary: str | None
    color_family: str | None
    fit: str | None
    min_price: float | None
    max_price: float | None
    exclude_keywords: list[str]
    boost_keywords: list[str]
    # NOTE (2026-05-20): `top_k` removed from LLM schema — Haiku 가 카드 캐러셀
    # 크기(5)를 따라하며 페이지네이션을 죽였음. dispatch 기본값 15 유지.


class RefineSearchArgs(TypedDict, total=False):
    # action enum mirrors v1 critique vocabulary
    action: Literal["broaden", "refine", "exclude", "cheaper", "color_swap"]
    exclude_brands: list[str]
    exclude_keywords: list[str]
    boost_keywords: list[str]
    color: str | None
    max_price: float | None
    min_price: float | None
    drop_min_price: bool
    # SPEC-SEARCH-V6-STYLE-WIRING text-only follow-up: optional 1-letter style
    # node override. Same digest is appended to the refine_search tool
    # description by style_node.warm_cache().
    style_node_primary: str | None


class UpdateTasteArgs(TypedDict, total=False):
    # source enum matches TasteSource (catalog #18)
    source: Literal["click", "onboard", "pinterest", "critique", "free_text", "no_click", "re_query"]
    brand_likes: list[str]
    brand_dislikes: list[str]
    keyword_likes: list[str]
    keyword_dislikes: list[str]


class AskUserClarificationArgs(TypedDict, total=False):
    axis: Literal["category_pick", "formality", "fit", "occasion", "subcategory_disambiguation", "generic_fallback"]
    options: list[str]
    prompt: str


class GetRecentHistoryArgs(TypedDict, total=False):
    n: int  # ≤ 20
    event_types: list[str]  # optional filter


class SuggestNextStepArgs(TypedDict, total=False):
    # SPEC-AGENT-V3-REACT Gap3 — proactive follow-up options card.
    kind: Literal["similar", "fit_change", "different_mood", "broaden", "generic"]
    options: list[str]
    prompt: str


class RespondArgs(TypedDict, total=False):
    # NOTE (SPEC-AGENT-V2-REACT cards-spam fix): `cards` is intentionally NOT a
    # field. The LLM (esp. nova-lite) cannot serialize search candidates and
    # char-exploded a markdown string into a per-character list → 1-char
    # Telegram sends + empty-string 400s. Cards are now sourced internally by
    # the respond tool from the turn's last search (`sess.last_results`).
    text: str


# ── Result TypedDicts (7 tools) ────────────────────────────────────────────


class AnalyzeImageResult(TypedDict, total=False):
    ok: bool
    error: str | None
    style_node_primary: str | None
    mood: list[str]
    palette: list[str]
    items_count: int
    subcategory: str | None
    fit: str | None
    color_family: str | None
    search_query: str | None


class SearchProductsResult(TypedDict, total=False):
    ok: bool
    error: str | None
    candidates_count: int
    top_candidates: list[dict[str, Any]]  # capped to 5 for LLM context


class RefineSearchResult(TypedDict, total=False):
    ok: bool
    error: str | None
    candidates_count: int
    top_candidates: list[dict[str, Any]]


class UpdateTasteResult(TypedDict, total=False):
    ok: bool
    error: str | None
    applied: bool


class AskUserClarificationResult(TypedDict, total=False):
    ok: bool
    error: str | None
    card_sent: bool
    axis: str


class GetRecentHistoryResult(TypedDict, total=False):
    ok: bool
    error: str | None
    events: list[dict[str, Any]]


class RespondResult(TypedDict, total=False):
    ok: bool
    error: str | None
    text_sent: bool
    cards_sent: int


class SuggestNextStepResult(TypedDict, total=False):
    ok: bool
    error: str | None
    card_sent: bool
    kind: str


# ── Metadata + REGISTRY ────────────────────────────────────────────────────


class ToolMetadata(TypedDict):
    name: str
    description: str
    args_typeddict: type
    result_typeddict: type
    dispatch_fn_path: str  # dotted import path: "app.agents.tools.X:dispatch"
    langfuse_span_tag: str
    side_effect_doc: str
    terminates_loop: bool  # True only for `respond`


REGISTRY: dict[str, ToolMetadata] = {
    "analyze_image": {
        "name": "analyze_image",
        "description": (
            "Analyze the user's fashion image and return structured style info. "
            "Use when the user provides a photo or Pinterest link and Vision hasn't run. "
            "Takes NO arguments — do NOT provide an image_url; the tool sources the "
            "resolved image internally from session state."
        ),
        "args_typeddict": AnalyzeImageArgs,
        "result_typeddict": AnalyzeImageResult,
        "dispatch_fn_path": "app.agents.tools.analyze_image:dispatch",
        "langfuse_span_tag": "tool.analyze_image",
        "side_effect_doc": "Calls Vision LLM (LiteLLM). SSRF-guarded URL validation.",
        "terminates_loop": False,
    },
    "search_products": {
        "name": "search_products",
        "description": (
            "Search the 78k-product catalog. Provide `text_query` plus optional "
            "filters (style_node_primary, color_family, fit, price). Do "
            "NOT provide an image_url — the tool handles imagery internally "
            "from session state. Returns top candidates with brand/title/price.\n"
            "\n"
            "[TEXT_QUERY CANONICAL FORM — REQUIRED for embedding cache stability]\n"
            "Always produce text_query in this exact shape:\n"
            '  "{color} {fit} {garment} {gender}"\n'
            "Rules:\n"
            "  - ENGLISH ONLY, lowercase, space-separated.\n"
            "  - No articles (a / an / the), no prepositions (for / with), no "
            "possessives (men's → men, women's → women).\n"
            "  - Garment must be singular and use the most common term: "
            "'t-shirt' (not 'tee' / 'tees' / 'tshirt'), 'jeans' (not 'denim "
            "pants'), 'sneakers' (not 'trainers'), 'hoodie' (not 'hooded "
            "sweatshirt'), 'sweater' (not 'jumper' / 'knit top').\n"
            "  - Color: prefer 'grey' over 'gray', 'beige' over 'tan/khaki' "
            "unless explicitly khaki.\n"
            "  - Fit: one of 'fitted' / 'slim' / 'regular' / 'loose' / 'oversized' "
            "/ 'wide' / 'straight' / 'cropped'. Omit if unspecified.\n"
            "  - Gender: include 'men' / 'women' ONLY when there's an explicit signal (user text or "
            "picked Vision item — never flip it). When NO signal exists, OMIT gender (the system "
            "appends 'unisex' downstream). Never guess 'women'.\n"
            "  - Omit any field you don't have — never pad with vague words.\n"
            "Examples (gender word ONLY when there's a signal; else omit → system adds unisex):\n"
            '  ✅ "grey fitted t-shirt men"               (user said men)\n'
            '  ✅ "black wide jeans"                       (no signal → omit; system → unisex)\n'
            '  ✅ "beige oversized hoodie"                 (no signal → omit)\n'
            '  ✅ "leather loafers men"                    (picked vision item said men)\n'
            '  ❌ "black wide jeans women"                 (invented gender — no signal existed)\n'
            '  ❌ "Grey Fitted T-Shirt for Men"          (caps, preposition)\n'
            "  ❌ \"men's grey tee that's fitted\"          (possessive, clause)\n"
            '  ❌ "fitted grey t-shirt for men"           (wrong order)\n'
            '  ❌ "a pair of denim jeans"                 (article, redundant)'
        ),
        "args_typeddict": SearchProductsArgs,
        "result_typeddict": SearchProductsResult,
        "dispatch_fn_path": "app.agents.tools.search_products:dispatch",
        "langfuse_span_tag": "tool.search_products",
        "side_effect_doc": "DB RPC + Modal embedding call.",
        "terminates_loop": False,
    },
    "refine_search": {
        "name": "refine_search",
        "description": (
            "Refine a previous search using a critique delta (broaden, cheaper, exclude, etc.). "
            "Use after a search returned poor results."
        ),
        "args_typeddict": RefineSearchArgs,
        "result_typeddict": RefineSearchResult,
        "dispatch_fn_path": "app.agents.tools.refine_search:dispatch",
        "langfuse_span_tag": "tool.refine_search",
        "side_effect_doc": "Same as search_products. 1 retry budget per turn.",
        "terminates_loop": False,
    },
    "update_taste": {
        "name": "update_taste",
        "description": (
            "Persist user taste preferences (brand likes/dislikes, keyword affinities). "
            "Use after explicit user feedback like 'I love that brand'."
        ),
        "args_typeddict": UpdateTasteArgs,
        "result_typeddict": UpdateTasteResult,
        "dispatch_fn_path": "app.agents.tools.update_taste:dispatch",
        "langfuse_span_tag": "tool.update_taste",
        "side_effect_doc": "TasteProfile mutation (in-memory or Postgres).",
        "terminates_loop": False,
    },
    "ask_user_clarification": {
        "name": "ask_user_clarification",
        "description": (
            "Send the user an inline-keyboard card asking to clarify intent on one axis. "
            "`axis` MUST be EXACTLY one of these 6 strings (case-sensitive, no variants):\n"
            "  - 'category_pick'              — when user didn't say what garment (top/bottom/outer/dress/shoes/bag)\n"
            "  - 'formality'                  — casual vs business vs formal\n"
            "  - 'fit'                        — slim/regular/oversized/etc\n"
            "  - 'occasion'                   — daily/date/work/party/wedding/etc\n"
            "  - 'subcategory_disambiguation' — narrowing within a category (e.g. shirt: oxford vs linen vs flannel)\n"
            "  - 'generic_fallback'           — when none of the above fit (last resort)\n"
            "DO NOT invent axes like 'gender', 'wearer', 'mood', 'occasion & vibe', etc — they will be rejected."
        ),
        "args_typeddict": AskUserClarificationArgs,
        "result_typeddict": AskUserClarificationResult,
        "dispatch_fn_path": "app.agents.tools.ask_user_clarification:dispatch",
        "langfuse_span_tag": "tool.ask_user_clarification",
        "side_effect_doc": "Sends a Telegram message with InlineKeyboard.",
        "terminates_loop": False,
    },
    "get_recent_history": {
        "name": "get_recent_history",
        "description": (
            "Fetch this user's recent conversation events (last N entries). "
            "Use when current context is insufficient to decide intent."
        ),
        "args_typeddict": GetRecentHistoryArgs,
        "result_typeddict": GetRecentHistoryResult,
        "dispatch_fn_path": "app.agents.tools.get_recent_history:dispatch",
        "langfuse_span_tag": "tool.get_recent_history",
        "side_effect_doc": "Read-only SELECT on ai.log_conversation_event.",
        "terminates_loop": False,
    },
    "respond": {
        "name": "respond",
        "description": (
            "Send a natural-language reply to the user. Provide ONLY `text` "
            "(your written reply). Do NOT provide cards — product cards are "
            "attached automatically by the system from the most recent search "
            "in this conversation. ALWAYS terminates the agent loop."
        ),
        "args_typeddict": RespondArgs,
        "result_typeddict": RespondResult,
        "dispatch_fn_path": "app.agents.tools.respond:dispatch",
        "langfuse_span_tag": "tool.respond",
        "side_effect_doc": "Sends Telegram messages (text + optional product cards).",
        "terminates_loop": True,
    },
}

# SPEC-AGENT-V2-CLEANUP-001 — the 8th tool (`suggest_next_step`) is now
# ALWAYS registered (the AGENT_V3_PROACTIVE_ENABLED flag was removed). The
# ReAct agent is the permanent, only topology.
# @MX:NOTE: [AUTO] 8-tool registry — suggest_next_step is unconditional
REGISTRY["suggest_next_step"] = {
    "name": "suggest_next_step",
    "description": (
        "Proactively send the user a follow-up options card (similar items, "
        "fit change, different mood, broaden). Use when results are weak "
        "(candidates_count < 3) or to offer next steps. Does NOT terminate "
        "the loop — follow with `respond` once the user has options."
    ),
    "args_typeddict": SuggestNextStepArgs,
    "result_typeddict": SuggestNextStepResult,
    "dispatch_fn_path": "app.agents.tools.suggest_next_step:dispatch",
    "langfuse_span_tag": "tool.suggest_next_step",
    "side_effect_doc": "Sends a Telegram message with InlineKeyboard (reuses adapter).",
    "terminates_loop": False,
}

TOOL_NAMES: tuple[str, ...] = tuple(REGISTRY.keys())


# ── Args validation (REQ-AGENT-TOOL-DISPATCH-001) ─────────────────────────


def validate_args(tool_name: str, args: dict[str, Any]) -> tuple[bool, str | None]:
    """Lightweight TypedDict validation.

    Returns (ok, error_message). On invalid:
    - tool not in REGISTRY → (False, "unknown_tool")
    - args is not a dict → (False, "args_not_dict")
    - unknown keys present → (False, "unknown_keys: ...")
    - missing required keys (TypedDict `__required_keys__`) → (False, "missing_required: ...")

    Does not deeply check value types — TypedDict is structural and the LLM via
    OpenAI Tools API should already produce schema-compliant JSON. Defense in depth.
    """
    if tool_name not in REGISTRY:
        return False, f"unknown_tool: {tool_name}"
    if not isinstance(args, dict):
        return False, "args_not_dict"

    td = REGISTRY[tool_name]["args_typeddict"]
    allowed = set(getattr(td, "__annotations__", {}).keys())
    provided = set(args.keys())
    unknown = provided - allowed
    if unknown:
        return False, f"unknown_keys: {sorted(unknown)}"

    required = set(getattr(td, "__required_keys__", frozenset()))
    missing = required - provided
    if missing:
        return False, f"missing_required: {sorted(missing)}"

    # P1-5: lightweight isinstance enforcement for type-critical fields only.
    # An LLM passing `top_k: {"nested": 1}` or `n: "abc"` would otherwise pass
    # validation and crash deep in a caller. Not a full schema validator —
    # only the fields whose wrong type breaks a downstream call.
    # P1-C: `top_k`/`n` are int-only; `min_price`/`max_price` are typed
    # `float | None` (SearchProductsArgs/RefineSearchArgs) — the LLM
    # legitimately sends e.g. 59.99, so accept int OR float there.
    # bool is a subclass of int — reject it explicitly in both groups.
    #
    # AUTO-CAST (2026-05-20): Haiku 4.5 occasionally sends type-mismatched JSON
    # (`top_k="15"`, `boost_keywords="t-shirt"`, `min_price="50000"`). Strict
    # rejection burns a ReAct iter per occurrence — turns saw 3 consecutive
    # bad_args eating half the iter budget before search even started. Safe
    # coercions are done in-place; only genuinely unconvertible values reject.
    #
    # CRITICAL: never `list(v)` a string — `list("t-shirt")` explodes to
    # `["t","-","s","h","i","r","t"]` which contaminates the embedded query
    # (the bug the previous strict check was avoiding). Use `[v]` to wrap.
    for key in ("top_k", "n"):
        if key in args:
            v = args[key]
            if isinstance(v, bool):
                return False, f"bad_type: {key} must be int"
            if isinstance(v, int):
                continue
            if isinstance(v, float) and v.is_integer():
                args[key] = int(v)
                continue
            if isinstance(v, str):
                stripped = v.strip()
                if stripped.lstrip("-").isdigit():
                    args[key] = int(stripped)
                    continue
            return False, f"bad_type: {key} must be int"
    for key in ("min_price", "max_price"):
        if key in args:
            v = args[key]
            if isinstance(v, bool):
                return False, f"bad_type: {key} must be number"
            if isinstance(v, (int, float)):
                continue
            if isinstance(v, str):
                try:
                    args[key] = float(v.strip())
                    continue
                except (ValueError, AttributeError):
                    pass
            return False, f"bad_type: {key} must be number"
    for key in (
        "brand_likes",
        "brand_dislikes",
        "keyword_likes",
        "keyword_dislikes",
        "event_types",
        "options",
        # 2026-05-20: refine_search list fields. Without strict check, an LLM
        # passing a string ("t-shirt") flows through `list(...)` in dispatch
        # and explodes into per-character tokens (["t","-","s","h","i","r","t"]).
        # Auto-cast (2026-05-20): wrap single str in `[v]` so a lone keyword
        # passes safely; reject anything else (dict, int, etc.).
        "boost_keywords",
        "exclude_keywords",
    ):
        if key in args:
            v = args[key]
            if isinstance(v, list):
                continue
            if isinstance(v, str):
                # Lone non-empty string → 1-element list. Empty string drops the
                # field entirely (LLM occasionally sends "" as a no-op).
                stripped = v.strip()
                args[key] = [stripped] if stripped else []
                continue
            return False, f"bad_type: {key} must be list"

    # P0-1 (260521 V3 eval): Literal-value enforcement for type-critical enum
    # fields. The structural TypedDict check above accepts ANY string for
    # `axis`, then the dispatcher returns `invalid_axis:gender` and the LLM
    # often retries with the SAME invalid value (an info-poor reject loop).
    # Reject here so the validator error message itself spells out the valid
    # set — `bad_axis: 'gender' not in ['category_pick', ...]` — which the
    # ReAct loop returns to the LLM as result_summary, enabling self-correction
    # on the very next iter. Scoped to `ask_user_clarification` until other
    # tools demand the same.
    if tool_name == "ask_user_clarification" and "axis" in args:
        valid_axes = ("category_pick", "formality", "fit", "occasion", "subcategory_disambiguation", "generic_fallback")
        axis = args["axis"]
        if not isinstance(axis, str) or axis not in valid_axes:
            return False, f"bad_axis: {axis!r} not in {list(valid_axes)}"

    return True, None
