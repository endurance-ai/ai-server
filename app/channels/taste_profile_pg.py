"""Thin re-export shim (SPEC-ARCH-AI-001 PR4, REQ-AI-004).

PostgresTasteProfileStore moved VERBATIM to
app/infrastructure/memory/taste_profile_pg.py. The old import path is
aliased to the SAME module object via sys.modules (fully transparent).
"""

import sys

from app.infrastructure.memory import taste_profile_pg as _canonical

sys.modules[__name__] = _canonical
