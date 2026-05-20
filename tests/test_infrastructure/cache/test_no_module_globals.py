"""SPEC-CHAT-STATE-REDIS-001 — grep regression.

The legacy module-global dicts + their reset hook MUST be fully removed from
the app source tree (they live in `app/agents/tools/respond.py` pre-SPEC).
Kept in a sync-only file to avoid the autouse asyncio marker.
"""

from __future__ import annotations

import pathlib

_FORBIDDEN = (
    "_CARD_BATCH_CURSOR",
    "_LOGGED_IMPRESSION_IDS",
    "reset_card_batch_cursor_for_tests",
)


def test_no_module_global_chat_state_dicts_in_app():
    root = pathlib.Path(__file__).resolve().parents[3]
    offenders: list[str] = []
    for path in (root / "app").rglob("*.py"):
        try:
            src = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for token in _FORBIDDEN:
            if token in src:
                offenders.append(f"{path.relative_to(root)}: {token}")
    assert offenders == [], f"Forbidden module-global tokens still present: {offenders}"
