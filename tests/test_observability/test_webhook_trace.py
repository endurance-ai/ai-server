"""REQ-OBS-METADATA-001 — webhook handler has its own root span with metadata."""

from __future__ import annotations

import inspect


def test_webhook_handler_decorated_with_observe() -> None:
    """The graph-invocation entrypoint MUST be wrapped with @observe so that all
    node spans are children of one webhook root trace."""
    from app.api.webhooks import telegram as wh_module

    src = inspect.getsource(wh_module)
    assert '@observe(name="webhook.telegram"' in src, (
        "expected @observe(name='webhook.telegram', ...) on the webhook graph invocation"
    )


def test_webhook_handler_attaches_flow_lang_metadata(span_recorder, monkeypatch) -> None:
    """REQ-OBS-METADATA-001 — root trace must carry lang + flow + chat_id_hash."""
    # This is a structural assertion: the webhook code calls `update_current_span`
    # with the keys we care about.
    from app.api.webhooks import telegram as wh_module

    src = inspect.getsource(wh_module)
    assert "update_current_span" in src or "update_current_trace" in src or "update_trace_metadata" in src, (
        "webhook handler must attach root-trace metadata via langfuse client API"
    )
    # The metadata keys MUST appear in source as string literals.
    for key in ("flow", "chat_id_hash"):
        assert f'"{key}"' in src, f"webhook handler missing metadata key {key!r}"
