"""Contract tests for product outbound attribution."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api import products


@pytest.mark.asyncio
async def test_record_outbound_maps_session_id_to_thread_id(monkeypatch: pytest.MonkeyPatch):
    record_signal = AsyncMock(return_value=True)
    monkeypatch.setattr(products, "record_product_signal", record_signal)
    user_id = uuid4()
    session_id = uuid4()
    pool = object()

    response = await products.record_outbound(
        42,
        products.OutboundRequest(
            session_id=session_id,
            source="curation",
            section_id="editorial-summer",
        ),
        user_id=user_id,
        pool=pool,
    )

    assert response.recorded is True
    record_signal.assert_awaited_once()
    kwargs = record_signal.await_args.kwargs
    assert record_signal.await_args.args == (pool,)
    assert kwargs["user_id"] == user_id
    assert kwargs["product_id"] == 42
    assert kwargs["signal_type"] == "outbound"
    assert kwargs["metadata"] == {
        "thread_id": str(session_id),
        "source": "curation",
        "section_id": "editorial-summer",
    }


@pytest.mark.asyncio
async def test_record_outbound_defaults_source_and_allows_missing_session(monkeypatch: pytest.MonkeyPatch):
    record_signal = AsyncMock(return_value=False)
    monkeypatch.setattr(products, "record_product_signal", record_signal)

    response = await products.record_outbound(
        42,
        products.OutboundRequest(),
        user_id=uuid4(),
        pool=object(),
    )

    assert response.recorded is False
    assert record_signal.await_args.kwargs["metadata"] == {
        "thread_id": None,
        "source": "pdp",
        "section_id": None,
    }


def test_outbound_request_rejects_unknown_source():
    with pytest.raises(ValidationError):
        products.OutboundRequest(source="unknown")
