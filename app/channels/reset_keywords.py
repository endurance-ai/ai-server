"""SPEC-ONBOARD-LITE-001 — `/reset` keyword single source.

Repurposed from the retired SPEC-ONBOARD-CARDS-001 restart-keyword set.
`/reset` no longer re-enters an onboarding card flow (removed); it now
clears the caller's TasteProfile. Kept as a tiny standalone module so the
deletion of `onboarding_values.py` does not strand the predicate.
"""

from __future__ import annotations

# Hangul `\b` word boundary is unreliable — exact (stripped, lowercased)
# match only, mirroring the retired is_restart_keyword contract.
RESET_KEYWORDS: frozenset[str] = frozenset({"/reset", "취향 초기화", "reset taste"})


def is_reset_keyword(text: str | None) -> bool:
    """True iff `text` (stripped, casefolded) is an exact taste-reset trigger."""
    if not text:
        return False
    return text.strip().lower() in RESET_KEYWORDS
