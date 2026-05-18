"""SPEC-AGENT-V3-REACT / T2 — byte-identical regression guard (P0).

REQ-AGENT-V3-COMPAT-ALLOFF-001 / AC-X.1. The single core regression gate.

The frozen-baseline constants in this module were captured against untouched
V2 BEFORE any gap code existed. Every assertion runs with the 4 sub-flags
forced OFF (master V2 gate ON-equivalent — these helpers do not invoke the
loop, they snapshot the deterministic assembly inputs). A gap that mutates
any of these with its flag OFF is a defect, not a test that needs updating.

Run T2 fully and confirm green BEFORE T3+; re-run after every gap.
"""

from __future__ import annotations

import json

import pytest

from app.channels.persona import KIKO_PERSONA_SYSTEM_PROMPT
from app.core.config import settings
from tests.test_agent_v3 import _v2_baseline as bl

# Re-baselined 2026-05-18 for the SPEC-AGENT-V2-REACT persona-drift fix: the V2
# ReAct system prompt now embeds the CANONICAL kiko persona verbatim (shared
# with V1 `respond.py` via `app/channels/persona.py`) instead of a thin drifting
# "Voice:" one-liner. The persona enrichment is an INTENDED V2 base change — the
# frozen anchor below is updated once to the new V2 base, NOT patched per-gap.
#
# This anchor's job is "with all 4 V3 sub-flags OFF, system_content ==
# _SYSTEM_PROMPT (no V3 block appended)". That semantic is independently and
# more robustly guarded by test_gap1_memory.py / test_gap3_proactive.py, which
# compare the assembled system message to the LIVE `rl._SYSTEM_PROMPT`. To keep
# THIS frozen anchor from silently re-drifting from the real persona, the
# persona segment is referenced from the shared constant (single source of
# truth) rather than re-typed; only the V2 ReAct-operational text is literal.
_FROZEN_SYSTEM_PROMPT = (
    f"{KIKO_PERSONA_SYSTEM_PROMPT}\n\n"
    "OPERATING MODE — you operate as a ReAct agent: decide which tool to call at "
    "each step. ALWAYS end with the `respond` tool which sends the final "
    "natural-language reply to the user (written in the kiko voice and language "
    "rules above). `respond` takes ONLY a `text` argument — never pass cards or "
    "product data; the system attaches the search result cards automatically "
    "from the most recent search.\n\n"
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


@pytest.fixture(autouse=True)
def _force_all_off(monkeypatch):
    """4 sub-flags forced OFF for every assertion in this module."""
    monkeypatch.setattr(settings, "AGENT_V3_MEMORY_INJECTION_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "AGENT_V3_REFLEXION_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "AGENT_V3_PROACTIVE_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "AGENT_V3_DISLIKE_MEMORY_ENABLED", False, raising=False)


def test_system_prompt_byte_identical():
    """_SYSTEM_PROMPT must be byte-exact V2 (Gap1/Gap3 must not mutate it OFF)."""
    assert bl.system_prompt_snapshot() == _FROZEN_SYSTEM_PROMPT


def test_tool_names_seven_tool_exact():
    """REGISTRY is exactly the V2 7-tool, in order (Gap3 must not add OFF)."""
    assert bl.tool_names_snapshot() == bl.V2_TOOL_NAMES
    assert len(bl.tool_names_snapshot()) == 7


def test_registry_shape_stable():
    """Every tool's metadata projection is JSON-stable (no drift)."""
    shape = bl.registry_shape_snapshot()
    assert set(shape.keys()) == set(bl.V2_TOOL_NAMES)
    assert shape["respond"]["terminates_loop"] is True
    assert all(shape[n]["terminates_loop"] is False for n in bl.V2_TOOL_NAMES if n != "respond")
    # Round-trips through json — i.e. fully serializable / byte-comparable.
    json.dumps(shape, sort_keys=True)


@pytest.mark.parametrize("scenario", list(bl.scenario_states().keys()))
def test_user_message_byte_identical_per_scenario(scenario):
    """_build_user_message for the 6 AC-X.1 scenarios is byte-stable.

    Self-consistency snapshot: two invocations with the same input produce the
    identical string (deterministic), and the [USER INPUT — DATA ONLY] fence is
    preserved on every text-bearing scenario (security invariant).
    """
    snaps = bl.user_message_snapshots()
    again = bl.user_message_snapshots()
    assert snaps[scenario] == again[scenario]
    st, se = bl.scenario_states()[scenario]
    if st.message and st.message.text:
        assert "[USER INPUT — DATA ONLY]" in snaps[scenario]
        assert "[/USER INPUT]" in snaps[scenario]


def test_toolmessage_content_serialization_v2_format():
    """ToolMessage content == json.dumps(result, default=str)[:2000] (Gap2 OFF)."""
    result = {"ok": True, "candidates_count": 5, "top_candidates": [{"brand": "ami"}]}
    assert bl.toolmessage_content_v2(result) == json.dumps(result, default=str)[:2000]
    # 2000-char cap is part of the contract.
    big = {"ok": True, "blob": "x" * 5000}
    assert len(bl.toolmessage_content_v2(big)) == 2000


def test_taste_profile_field_set_is_v2_superset():
    """REQ-AGENT-V3-DISLIKE-SCHEMA-001 acceptance — TasteProfile field set is a
    V2 SUPERSET: every V2 field is present with its V2 type/default UNCHANGED,
    and the ONLY additions are the 2 Gap4 timestamp dicts (additive schema,
    not a behaviour change — both default to an empty dict so flag-OFF rows
    are byte-identical V2)."""
    snap = bl.taste_profile_field_snapshot()
    fields = set(snap.keys())
    # All V2 fields still present with identical type|default repr.
    assert bl.V2_TASTE_PROFILE_FIELDS <= fields
    for v2f in bl.V2_TASTE_PROFILE_FIELDS:
        assert snap[v2f] == bl.V2_TASTE_PROFILE_FIELD_REPRS[v2f]
    # Exactly the 2 Gap4 ts dicts added, both default_factory=dict.
    added = fields - bl.V2_TASTE_PROFILE_FIELDS
    assert added == {"disliked_brands_ts", "disliked_keywords_ts"}
    for f in added:
        assert snap[f] == "dict[str, float]|factory:dict"
