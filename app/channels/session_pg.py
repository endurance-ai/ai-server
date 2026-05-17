"""Thin re-export shim (SPEC-ARCH-AI-001 PR4, REQ-AI-004).

PostgresSessionStore moved VERBATIM to
app/infrastructure/memory/session_pg.py. The old import path is aliased to
the SAME module object via sys.modules (fully transparent relocation;
`_to_jsonable` and every other attribute resolve byte-identically).
"""

import sys

from app.infrastructure.memory import session_pg as _canonical

sys.modules[__name__] = _canonical
