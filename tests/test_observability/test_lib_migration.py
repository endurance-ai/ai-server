"""REQ-OBS-LIB-001 + REQ-OBS-MIGRATION-001 — v3 imports work, v2 paths are gone."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# langfuse uses pydantic.v1 internally, which is incompatible with Python 3.14+.
# The tests that directly import langfuse are skipped on Python 3.14+ until
# langfuse ships a pydantic-v1-free release.
_langfuse_importable = sys.version_info < (3, 14)
_skip_langfuse = pytest.mark.skipif(
    not _langfuse_importable,
    reason="langfuse pydantic.v1 internals incompatible with Python 3.14+",
)


@_skip_langfuse
def test_langfuse_v3_observe_importable() -> None:
    from langfuse import observe

    assert callable(observe)


@_skip_langfuse
def test_langfuse_v3_callback_handler_importable() -> None:
    from langfuse.langchain import CallbackHandler

    assert CallbackHandler is not None


@_skip_langfuse
def test_langfuse_v3_get_client_and_flush() -> None:
    from langfuse import get_client

    client = get_client()
    assert hasattr(client, "flush")
    assert hasattr(client, "update_current_span")


def test_no_v2_import_paths_in_app() -> None:
    """REQ-OBS-MIGRATION-001 — v2 import paths are gone from app/ only."""
    repo = Path(__file__).resolve().parents[2]
    # NOTE: we scan `app/` only — the test files themselves legitimately mention
    # these strings in test names/docstrings asserting their absence.
    # Constructed via concatenation to keep this very file grep-clean.
    needles = ["langfuse." + "decorators", "langfuse." + "callback"]
    for needle in needles:
        result = subprocess.run(
            ["grep", "-R", "-n", needle, str(repo / "app")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, f"Leftover v2 reference to {needle!r}:\n{result.stdout}"
