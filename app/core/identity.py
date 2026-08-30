"""Channel-neutral identity helpers for the consumer chat path.

The graph and a few legacy persistence helpers still use an integer key.  The
native app/web API, however, identifies callers with an authenticated UUID.
This module owns that compatibility mapping so it is not mistaken for a
Telegram chat identifier at API boundaries.
"""

from uuid import UUID


def uuid_to_session_key(value: UUID) -> int:
    """Return a stable positive integer for an existing int-keyed store."""

    return abs(int.from_bytes(value.bytes[:8], "big")) % (2**62)


def user_id_to_session_key(user_id: UUID) -> int:
    """Return the stable integer key used by legacy graph/session helpers.

    The mapping is deterministic and bounded to the positive signed 63-bit
    range expected by the existing Redis/Postgres helpers.  It does not expose
    the UUID or depend on a messaging provider.
    """

    return uuid_to_session_key(user_id)
