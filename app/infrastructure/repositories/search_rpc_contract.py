"""search_products_v6 RPC response contract (SPEC-ARCH-AI-001 PR6, REQ-AI-006;
re-pointed by SPEC-SEARCH-V6-001).

A documented Pydantic model for the row shape `search_products_v6` returns.
The repository validates rows against this contract BEFORE scoring/diversify
so contract drift surfaces as a structured error instead of silent
malformed scoring.

v6 rows carry `distance` (cosine, ASC = better; the RPC already returns rows
ordered by distance ASC) and a `degraded` boolean (true = style-node filter
dropped → category-only fallback; observability only). They no longer carry
`score`/`dense_rank`/`sparse_rank` (v5 + pgroonga RRF retired). `products.id`
is now bigint, so PostgREST may return `id` as int or str — the contract
keeps it permissive.

The contract is intentionally PERMISSIVE: every field except `id` is optional
(the runner does `.get(...)` with defaults). Validation NEVER mutates or
coerces the rows that flow onward — the original dicts pass through untouched;
the model is used only as a shape gate. The ONLY new behavior is the drift
error branch.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class SearchRpcRowContract(BaseModel):
    """Documented shape of one `search_products_v6` row (SPEC-SEARCH-V6-001).

    Only `id` is required (the runner does an unguarded `row["id"]`, so a
    missing id was always an error — this contract surfaces it as a structured
    error before scoring instead of a raw KeyError mid-pipeline). `id` stays
    `int | str` (bigint; PostgREST may return either). Every other field is
    optional with permissive types so no row the happy path accepts is
    rejected. `extra="allow"` keeps unknown columns from tripping drift
    (forward-compatible).
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
    distance: float | None = None
    degraded: bool | None = None


class RpcContractError(RuntimeError):
    """Raised when `search_products_v6` rows violate the documented contract.

    Structured (carries the offending row index + the pydantic error) so the
    failure is diagnosable rather than a silent malformed-scoring downstream.
    """

    def __init__(self, row_index: int, detail: str) -> None:
        self.row_index = row_index
        self.detail = detail
        super().__init__(f"search_products_v6 contract drift at row {row_index}: {detail}")


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
