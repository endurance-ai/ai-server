"""Thin re-export shim (SPEC-ARCH-AI-001 PR4, REQ-AI-004).

The session store moved VERBATIM to app/infrastructure/memory/session.py.
This shim aliases the OLD import path to the SAME module object via
sys.modules so the relocation is fully transparent: every attribute
(including the private `_store`/`_factory` globals that
tests/test_implicit_feedback/conftest.py monkeypatches), every class
identity, and every `from app.channels.session import ...` resolve
byte-identically against the single canonical module.
"""

import sys

from app.infrastructure.memory import session as _canonical

sys.modules[__name__] = _canonical
