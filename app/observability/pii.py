"""SPEC-OBSERVABILITY-002 / REQ-OBS-PII-001 — single canonical PII helper.

Raw `chat_id` / `from_user_id` MUST NEVER appear in any Langfuse span attribute,
metadata field, or log line. This module is the ONLY supported conversion path
from raw identifier to span-safe identifier.

@MX:ANCHOR: [AUTO] sha256-prefix-16 hashing is a security-critical primitive
@MX:REASON: All Langfuse spans, log lines, and trace metadata that need to
reference a user identity go through this helper. Changing the algorithm
breaks searchability across historical traces.
"""

from __future__ import annotations

import hashlib


def hash_id(value: int | str | None) -> str | None:
    """Return sha256(str(value)).hexdigest()[:16], or None when input is None.

    Deterministic — repeated calls with the same input always return the same
    output. 16 hex chars = 64 bits of entropy, sufficient to avoid collisions
    in our user-base size while keeping the field short for log readability.
    """
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
