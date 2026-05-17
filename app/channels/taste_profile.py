"""Thin re-export shim (SPEC-ARCH-AI-001 PR4, REQ-AI-004).

The taste profile moved VERBATIM to
app/infrastructure/memory/taste_profile.py. The old import path is aliased
to the SAME module object via sys.modules so the private `_store`/`_factory`
globals (monkeypatched by tests/test_implicit_feedback/conftest.py) and all
public names resolve byte-identically.
"""

import sys

from app.infrastructure.memory import taste_profile as _canonical

sys.modules[__name__] = _canonical
