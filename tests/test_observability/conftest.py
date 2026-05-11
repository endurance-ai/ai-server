"""Shared fixtures for SPEC-OBSERVABILITY-002 tests.

Captures Langfuse v3 spans via a lightweight in-process recorder that monkeypatches
the `@observe` decorator path used by `app.observability.langfuse`. Tests should
NOT require a real Langfuse server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class RecordedSpan:
    name: str | None
    as_type: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    args_repr: str = ""
    kwargs_keys: tuple[str, ...] = ()


@dataclass
class SpanRecorder:
    spans: list[RecordedSpan] = field(default_factory=list)
    metadata_updates: list[dict[str, Any]] = field(default_factory=list)
    current: list[RecordedSpan] = field(default_factory=list)

    def reset(self) -> None:
        self.spans.clear()
        self.metadata_updates.clear()
        self.current.clear()


@pytest.fixture
def span_recorder(monkeypatch) -> SpanRecorder:
    """Patch `app.observability.langfuse.observe` to record span emissions.

    The recorder works regardless of whether Langfuse is enabled — it replaces
    the wrapper produced by our module-level `observe()` factory.
    """
    rec = SpanRecorder()

    from app.observability import langfuse as obs

    real_observe = obs.observe

    def fake_observe(name: str | None = None, **kwargs: Any):
        as_type = kwargs.get("as_type")

        def decorator(fn):
            import functools

            @functools.wraps(fn)
            async def wrapper(*args, **kw):
                span = RecordedSpan(
                    name=name,
                    as_type=as_type,
                    args_repr=repr(args)[:200],
                    kwargs_keys=tuple(kw.keys()),
                )
                rec.spans.append(span)
                rec.current.append(span)
                try:
                    return await fn(*args, **kw)
                finally:
                    rec.current.pop()

            return wrapper

        return decorator

    monkeypatch.setattr(obs, "observe", fake_observe)

    # Also patch update_trace_metadata helper if defined.
    def fake_update_current_span(metadata: dict[str, Any] | None = None, **kwargs: Any) -> None:
        if metadata:
            rec.metadata_updates.append(metadata)
            if rec.current:
                rec.current[-1].metadata.update(metadata)

    if hasattr(obs, "update_current_span"):
        monkeypatch.setattr(obs, "update_current_span", fake_update_current_span)

    yield rec

    # Restore
    monkeypatch.setattr(obs, "observe", real_observe)
