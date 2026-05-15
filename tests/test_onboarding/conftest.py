"""Conftest for onboarding tests.

We deliberately do NOT auto-load `tests.test_memory_pg.conftest` because that
conftest's session-scoped `pg_container` fixture eagerly skips when Docker is
unavailable, which cascades to all tests in this directory (even those that
don't need Postgres).

Postgres-bound tests in this directory should rely on the
`tests/test_memory_pg/` infrastructure being invoked directly when needed.
"""

from __future__ import annotations
