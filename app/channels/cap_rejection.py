"""Cap-box rejection keyword detector.

Detects explicit user rejection of the membership prompt ("안 써", "관심 없어",
"no thanks", ...) so the webhook can flip into 24h silence mode and stop
re-firing the cap boxes on every subsequent message.

Matching is intentionally simple — short normalized-text contains-check
against a curated KO/EN keyword set. False positives (e.g. user typing
"안 써본 브랜드 추천해줘") matter little here because the only consequence
is 24h of cap-box silence; the user can still trigger the membership URL
by tapping the previously-sent button. False negatives (slang we missed)
just preserve current behaviour.
"""

from __future__ import annotations

# Curated rejection phrases. Lower-cased + whitespace-stripped on lookup.
# Keep short — long phrases hurt recall; long enough to avoid common false
# positives on single particles.
_REJECTION_PHRASES: frozenset[str] = frozenset(
    {
        # KO — refusal / disinterest
        "안 써",
        "안써",
        "안 할래",
        "안할래",
        "안 해",
        "안해",
        "관심 없어",
        "관심없어",
        "관심 없음",
        "관심없음",
        "필요 없어",
        "필요없어",
        "필요 없음",
        "필요없음",
        "돈 안 내",
        "돈 안내",
        "돈안내",
        "유료 싫어",
        "유료싫어",
        "결제 안 해",
        "결제 안해",
        "결제안해",
        "싫어",
        "됐어",
        "됐다",
        "그만",
        "그만해",
        "닥쳐",
        # EN — refusal / disinterest
        "no thanks",
        "no thank you",
        "not interested",
        "don't want",
        "dont want",
        "won't pay",
        "wont pay",
        "not paying",
        "no pay",
        "stop",
        "shut up",
        "go away",
        "leave me alone",
    }
)


def is_rejection(text: str | None) -> bool:
    """Return True iff `text` looks like an explicit cap-box rejection.

    Matching: lowercase + collapse-whitespace, then contains-check against
    `_REJECTION_PHRASES`. Empty / None / overly-long messages (> 80 chars —
    almost certainly a real query, not a one-liner refusal) return False.
    """
    if not text:
        return False
    if len(text) > 80:
        return False
    norm = " ".join(text.lower().split())
    if not norm:
        return False
    return any(p in norm for p in _REJECTION_PHRASES)
