"""Net (4 — NEW, PR6) -- search_products_v5 RPC contract drift error path.

SPEC-ARCH-AI-001 REQ-AI-006. This is a NEW net file (does NOT edit the 3
existing golden nets). It locks the ONLY new behavior PR6 adds: a structured
RpcContractError when the RPC returns a row that violates the documented
contract, instead of silent malformed scoring downstream.

The happy-path equivalence (contract accepts every shape the pre-PR6 runner
accepted: absent score/brand, id as int or str, unknown extra columns) is
already locked byte-identically by Net(1)/Net(3) — those remain unchanged.
"""

from __future__ import annotations

import pytest

from app.infrastructure.repositories.search_rpc_contract import (
    RpcContractError,
    SearchRpcRowContract,
    validate_rpc_rows,
)


# ── happy: every shape the pre-PR6 runner accepted passes, rows untouched ──
def test_contract_accepts_absent_score_and_brand():
    rows = [
        {"id": "p7", "name": "Item 7", "platform": "web"},  # score absent
        {"id": "p9", "name": "Item 9", "score": 0.86},  # brand absent
        {"id": 12, "brand": "uniqlo", "score": 0.83},  # id as int
        {"id": "p0", "brand": "uniqlo", "score": 0.95, "unknown_col": "x"},  # extra
    ]
    out = validate_rpc_rows(rows)
    # ORIGINAL rows returned untouched (no coercion) -> happy path identical.
    assert out is rows
    assert out[0] == {"id": "p7", "name": "Item 7", "platform": "web"}
    assert "unknown_col" in out[3]


def test_contract_empty_rows_ok():
    assert validate_rpc_rows([]) == []


# ── drift: structured error (NEW PR6 behavior, not silent malformed) ───────
def test_contract_drift_missing_id_raises_structured():
    rows = [{"id": "p0", "score": 0.9}, {"name": "no id here", "score": 0.8}]
    with pytest.raises(RpcContractError) as ei:
        validate_rpc_rows(rows)
    assert ei.value.row_index == 1
    assert "row 1" in str(ei.value)


def test_contract_drift_non_numeric_score_raises():
    rows = [{"id": "p0", "score": "not-a-number"}]
    with pytest.raises(RpcContractError) as ei:
        validate_rpc_rows(rows)
    assert ei.value.row_index == 0


def test_contract_model_permissive_optional_fields():
    # Only id is required; everything else optional (matches pre-PR6
    # runner .get(...) defaults). Unknown columns allowed (forward-compat).
    m = SearchRpcRowContract.model_validate({"id": 1, "future_col": True})
    assert m.id == 1
    assert m.brand is None and m.score is None
