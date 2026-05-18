"""Shared characterization fixtures for SPEC-ARCH-AI-001 PRESERVE net
(v6-migrated by SPEC-SEARCH-V6-001).

Reuses the proven monkeypatch seams from tests/test_pipeline_with_enhance.py:
- app.pipeline.embed.EmbedProvider.embed_image_url  (AsyncMock fixed vector)
- app.pipeline.embed.EmbedProvider.embed_text       (AsyncMock fixed vector —
  v6 text-only path; same seam parity as embed_image_url)
- app.pipeline.search.SupabaseProvider.rpc          (capture/stub callable)

The embedding vector is intentionally [0.1234567] * 768 so that the pgvector
`:.7f` formatting in embedding_to_pgvector is exercised (0.1234567 ->
"0.1234567" with no rounding/padding drift).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

# 768-dim fixed embedding. 0.1234567 has exactly 7 fractional digits so the
# `f"{v:.7f}"` formatter neither rounds nor pads -> "[0.1234567,0.1234567,...]".
FIXED_EMBED_DIM = 768
FIXED_EMBED_VALUE = 0.1234567
FIXED_EMBEDDING: list[float] = [FIXED_EMBED_VALUE] * FIXED_EMBED_DIM


class RPCCapture:
    """SupabaseProvider.rpc stub that records (fn_name, params) and returns a
    fixed row list. Mirrors _RPCCapture in tests/test_pipeline_with_enhance.py
    but with a configurable return payload.
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = [] if rows is None else rows

    async def __call__(self, fn_name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((fn_name, params))
        return self._rows


@pytest.fixture
def fixed_embed(monkeypatch):
    """Patch EmbedProvider.embed_image_url AND embed_text -> fixed 768-dim
    vector (v6 text-only path uses embed_text; same seam parity)."""
    monkeypatch.setattr(
        "app.pipeline.embed.EmbedProvider.embed_image_url",
        AsyncMock(return_value=list(FIXED_EMBEDDING)),
    )
    monkeypatch.setattr(
        "app.pipeline.embed.EmbedProvider.embed_text",
        AsyncMock(return_value=list(FIXED_EMBEDDING)),
    )
    return list(FIXED_EMBEDDING)


@pytest.fixture
def rpc_capture(monkeypatch):
    """Patch SupabaseProvider.rpc with an empty-return capture by default.

    Tests that need rows install their own RPCCapture via patch_rpc().
    """
    cap = RPCCapture([])
    monkeypatch.setattr("app.pipeline.search.SupabaseProvider.rpc", cap)
    return cap


@pytest.fixture
def patch_rpc(monkeypatch):
    """Factory: install an RPCCapture returning the supplied rows."""

    def _install(rows: list[dict[str, Any]]) -> RPCCapture:
        cap = RPCCapture(rows)
        monkeypatch.setattr("app.pipeline.search.SupabaseProvider.rpc", cap)
        return cap

    return _install


@pytest.fixture(autouse=True)
def _enhance_disabled(monkeypatch):
    """Deterministic: force ENHANCE_QUERY_ENABLED=False for the whole net so
    no LLM call ever happens and query_text falls back to raw (search_query_ko
    or search_query). Individual tests may flip parallel/enhance explicitly.
    """
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "ENHANCE_QUERY_ENABLED", False)
    yield
