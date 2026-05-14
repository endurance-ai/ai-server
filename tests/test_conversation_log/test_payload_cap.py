"""SPEC-CONVERSATION-LOG-001 / REQ-LOG-PAYLOAD-CAP-001 — payload truncation caps.

- str → 2048 chars (tail truncate)
- list → 50 items (tail drop)
- dict → 100 keys (sort by key; for `taste_update.{keywords,brands}_delta`
  numeric-valued dicts → sort by value desc, keep top)
"""

from __future__ import annotations

from app.observability.conversation_log import _truncate


def test_string_capped_to_2048_chars():
    long = "x" * 5000
    out = _truncate({"text": long})
    assert len(out["text"]) == 2048
    assert out["text"] == "x" * 2048


def test_list_capped_to_50_items():
    long = list(range(200))
    out = _truncate({"items": long})
    assert len(out["items"]) == 50
    assert out["items"] == list(range(50))


def test_dict_capped_to_100_keys_sorted_by_key():
    big = {f"k{i:04d}": i for i in range(250)}
    out = _truncate({"misc": big})
    assert len(out["misc"]) == 100
    # Lexicographic order of keys (deterministic drop) — first 100 zero-padded.
    expected_keys = sorted([f"k{i:04d}" for i in range(250)])[:100]
    assert list(out["misc"].keys()) == expected_keys


def test_taste_update_keywords_delta_drops_lowest_weight():
    """Numeric-weighted dict under taste_update — sort by value desc, top 100."""
    # 150 keys with weights: smaller suffix → bigger weight
    weights = {f"kw{i:03d}": 1000 - i for i in range(150)}
    out = _truncate(
        {"source": "free_text", "keywords_delta": {"liked_added": weights}},
        event_type="taste_update",
    )
    inner = out["keywords_delta"]["liked_added"]
    assert len(inner) == 100
    # Top 100 should be weights 1000 down to 901 (i=0..99).
    assert min(inner.values()) == 901
    assert max(inner.values()) == 1000


def test_taste_update_brands_delta_also_drops_lowest():
    weights = {f"b{i}": float(200 - i) for i in range(120)}
    out = _truncate(
        {"source": "click", "brands_delta": {"liked_added": weights}},
        event_type="taste_update",
    )
    inner = out["brands_delta"]["liked_added"]
    assert len(inner) == 100
    assert min(inner.values()) == 101.0


def test_nested_list_inside_dict_recurses():
    out = _truncate({"items": [{"text": "y" * 5000}]})
    assert len(out["items"][0]["text"]) == 2048


def test_primitives_passthrough():
    out = _truncate({"n": 42, "ok": True, "score": 3.14})
    assert out == {"n": 42, "ok": True, "score": 3.14}


def test_short_inputs_unchanged():
    out = _truncate({"text": "hi", "items": [1, 2, 3]})
    assert out == {"text": "hi", "items": [1, 2, 3]}
