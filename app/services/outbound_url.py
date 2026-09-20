"""Partner attribution for user-facing outbound product URLs."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SLOWSTEADYCLUB_HOSTS = frozenset({"slowsteadyclub.com", "www.slowsteadyclub.com"})
_SLOWSTEADYCLUB_UTM = (
    ("utm_source", "kiko"),
    ("utm_medium", "referral"),
    ("utm_campaign", "slowsteadyclub"),
)
_SLOWSTEADYCLUB_UTM_KEYS = frozenset(key for key, _ in _SLOWSTEADYCLUB_UTM)


def with_partner_attribution(raw_url: str) -> str:
    """Return an outbound URL with the configured partner attribution.

    Catalog URLs remain canonical in storage. Attribution is added only when
    a URL leaves the service, preserving unrelated query parameters and the
    fragment. Existing partner UTM values are replaced instead of duplicated.
    """
    try:
        parts = urlsplit(raw_url)
    except ValueError:
        return raw_url

    if (parts.hostname or "").lower() not in _SLOWSTEADYCLUB_HOSTS:
        return raw_url

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in _SLOWSTEADYCLUB_UTM_KEYS
    ]
    query.extend(_SLOWSTEADYCLUB_UTM)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
