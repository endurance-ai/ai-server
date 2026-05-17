"""search_products_v5 RPC response contract (SPEC-ARCH-AI-001 PR6, REQ-AI-006).

A documented Pydantic model for the row shape `search_products_v5` returns.
The repository validates rows against this contract BEFORE scoring/diversify
so contract drift surfaces as a structured error instead of silent
malformed scoring.

[HARD] The happy path is byte-identical (REQ-AI-007). The contract is
intentionally PERMISSIVE: it accepts every shape the pre-PR6 runner already
accepted — including rows with `score` absent (runner default 0.0), `brand`
absent (runner default ""), and `id` as int OR str (runner does
`str(row["id"])`). Validation NEVER mutates or coerces the rows that flow
onward — the original dicts are passed through untouched; the model is used
only as a shape gate. The ONLY new behavior is the drift error branch.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class SearchRpcRowContract(BaseModel):
    """Documented shape of one `search_products_v5` row.

    Only `id` is required (the pre-PR6 code already does an unguarded
    `row["id"]`, so a missing id was always an error — this contract just
    surfaces it as a structured error before scoring instead of a raw
    KeyError mid-pipeline). Every other field is optional with permissive
    types so no row the happy path accepted is rejected. `extra="allow"`
    keeps unknown columns from tripping drift (forward-compatible).
    """

    model_config = ConfigDict(extra="allow")

    id: str | int
    brand: str | None = None
    name: str | None = None
    price: int | float | None = None
    image_url: str | None = None
    product_url: str | None = None
    platform: str | None = None
    subcategory: str | None = None
    score: int | float | None = None
    dense_rank: int | None = None
    sparse_rank: int | None = None


class RpcContractError(RuntimeError):
    """Raised when `search_products_v5` rows violate the documented contract.

    Structured (carries the offending row index + the pydantic error) so the
    failure is diagnosable rather than a silent malformed-scoring downstream.
    """

    def __init__(self, row_index: int, detail: str) -> None:
        self.row_index = row_index
        self.detail = detail
        super().__init__(f"search_products_v5 contract drift at row {row_index}: {detail}")


def validate_rpc_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate each row against the contract; return the ORIGINAL rows
    untouched on success (no coercion -> happy path byte-identical).

    Raises RpcContractError on the first drifting row.
    """
    for idx, row in enumerate(rows):
        try:
            SearchRpcRowContract.model_validate(row)
        except Exception as exc:  # noqa: BLE001 — re-wrapped as structured error
            raise RpcContractError(idx, str(exc)) from exc
    return rows


__all__ = [
    "SearchRpcRowContract",
    "RpcContractError",
    "validate_rpc_rows",
]
