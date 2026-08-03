"""Net (4 — PR6) -- search_products_v6 RPC contract drift error path
(re-pointed by SPEC-SEARCH-V6-001).

SPEC-ARCH-AI-001 REQ-AI-006. Locks the structured RpcContractError raised
when the v6 RPC returns a row violating the documented contract, instead of
silent malformed scoring downstream.

v6 row shape: id (bigint -> int or str), brand/name/image_url/product_url/
platform/subcategory (str|None), price (int|float|None), distance
(float|None), degraded (bool|None). No score/dense_rank/sparse_rank. Unknown
extra columns allowed (forward-compat). Only `id` is required.
"""

from __future__ import annotations

import pytest

from app.infrastructure.repositories.search_rpc_contract import (
    RpcContractError,
    SearchRpcRowContract,
    validate_rpc_rows,
)


# ── happy: every shape the v6 runner accepts passes, rows untouched ────────
def test_contract_accepts_v6_rows_untouched():
    rows = [
        {"id": 7, "name": "Item 7", "platform": "web"},  # distance absent
        {"id": 9, "name": "Item 9", "distance": 0.14},  # brand absent
        {"id": "12", "brand": "uniqlo", "distance": 0.2},  # id as str (bigint)
        {"id": 1, "distance": 0.05, "degraded": True, "unknown_col": "x"},  # extra + degraded
    ]
    out = validate_rpc_rows(rows)
    # ORIGINAL rows returned untouched (no coercion) -> happy path identical.
    assert out is rows
    assert out[0] == {"id": 7, "name": "Item 7", "platform": "web"}
    assert "unknown_col" in out[3]
    assert out[3]["degraded"] is True


def test_contract_empty_rows_ok():
    assert validate_rpc_rows([]) == []


# ── drift: structured error (surface, not silent malformed) ────────────────
def test_contract_drift_missing_id_raises_structured():
    rows = [{"id": 1, "distance": 0.1}, {"name": "no id here", "distance": 0.2}]
    with pytest.raises(RpcContractError) as ei:
        validate_rpc_rows(rows)
    assert ei.value.row_index == 1
    assert "row 1" in str(ei.value)
    assert "search_products_v6" in str(ei.value)


def test_contract_drift_non_numeric_distance_raises():
    rows = [{"id": 1, "distance": "not-a-number"}]
    with pytest.raises(RpcContractError) as ei:
        validate_rpc_rows(rows)
    assert ei.value.row_index == 0


def test_contract_model_permissive_optional_fields():
    # Only id is required; everything else optional (matches the v6 runner
    # .get(...) defaults). Unknown columns allowed (forward-compat). id stays
    # int|str (bigint -> PostgREST may return either).
    m = SearchRpcRowContract.model_validate({"id": 1, "future_col": True})
    assert m.id == 1
    assert m.brand is None and m.distance is None and m.degraded is None
    m2 = SearchRpcRowContract.model_validate({"id": "98765432101"})
    assert m2.id == "98765432101"
