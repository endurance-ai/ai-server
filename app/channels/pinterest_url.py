"""Pinterest URL classifier — 4-way tagged union.

SPEC-ONBOARD-CARDS-001 / REQ-ONBOARD-PINTEREST-002 + REQ-ONBOARD-SEC-001.

자유 텍스트 입력에서 Pinterest URL 들을 추출하고 PIN / BOARD / PROFILE / NONE
4-bucket 으로 분류한다. 우선순위: PIN > BOARD > PROFILE.

Host validation 은 반드시 `urllib.parse.urlsplit` 으로 host 를 추출한 뒤
allowlist regex 로 검증 — 단순 substring 매칭 시 `pinterest.com.evil.com`
같은 spoofing 을 막을 수 없다 (REQ-ONBOARD-SEC-001).

@MX:SPEC: SPEC-ONBOARD-CARDS-001
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


# ──────────────────────────────────────────────────────────────────────────
# Tagged union — `isinstance` discrimination
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PinInputPins:
    """One or more `/pin/{id}/` URLs. Mode C — link_resolver batch resolve."""

    urls: tuple[str, ...]
    truncated: bool = False


@dataclass(frozen=True)
class PinInputBoard:
    """`/{user}/{board}/` URL. Mode A — Apify board scrape."""

    url: str


@dataclass(frozen=True)
class PinInputProfile:
    """`/{user}/` URL. Mode B — Apify profile scrape."""

    url: str


@dataclass(frozen=True)
class PinInputNone:
    """No valid Pinterest URL recognized in input text."""


PinInput = PinInputPins | PinInputBoard | PinInputProfile | PinInputNone


# Convenience exports — alternate names for ergonomic destructuring at callsite.
BOARD = PinInputBoard
PROFILE = PinInputProfile
PINS = PinInputPins
NONE = PinInputNone


# ──────────────────────────────────────────────────────────────────────────
# Regex — host allowlist + path shape recognizers
# ──────────────────────────────────────────────────────────────────────────
# Host allowlist (REQ-ONBOARD-PINTEREST-002 / REQ-ONBOARD-SEC-001):
#   pinterest.com  /  www.pinterest.com  /  kr.pinterest.com  (any 2-letter cc)
#   pin.it
# NOT allowed: pinterest.com.evil.com, evil-pinterest.com, IDN homographs.
_HOST_RE = re.compile(r"^(?:[a-z]{2}\.)?pinterest\.com$|^pin\.it$|^www\.pinterest\.com$")

# Path shape — /pin/{digits}/ — accepts trailing slash optional
_PIN_PATH_RE = re.compile(r"^/pin/\d+/?$")
# `pin.it/abc123` style — anything under pin.it host is treated as a pin shortlink
_PIN_IT_PATH_RE = re.compile(r"^/[A-Za-z0-9_-]+/?$")
# Board: /{user}/{board}/  (exactly 2 segments, no digits-only path)
_BOARD_PATH_RE = re.compile(r"^/[^/]+/[^/]+/?$")
# Profile: /{user}/  (exactly 1 segment)
_PROFILE_PATH_RE = re.compile(r"^/[^/]+/?$")

# Bare URL extractor — matches http(s) URLs OR `pinterest.com/...` / `pin.it/...`
# bareword fragments. Trailing punctuation (`.`, `,`, `)`) is stripped at the
# tokenization layer.
#
# Left-anchor: either start-of-string, whitespace, or a scheme prefix MUST
# precede the host. This blocks `evil-pinterest.com/...` from being chopped
# into a spurious match on the trailing `pinterest.com/...` substring
# (REQ-ONBOARD-SEC-001).
_URL_TOKEN_RE = re.compile(
    r"(?:^|(?<=\s))"
    r"(?:https?://|//)?(?:[a-z]{2}\.)?(?:www\.)?(?:pinterest\.com|pin\.it)(?:/[^\s]*)?",
    re.IGNORECASE | re.MULTILINE,
)


def _normalize_scheme(raw: str) -> str | None:
    """Promote bare URL to https; reject http:// (REQ-ONBOARD-SEC-001).

    Returns the normalized URL or `None` if the scheme cannot be made safe.
    """
    s = raw.strip().rstrip(".,)")
    lower = s.lower()
    if lower.startswith("https://"):
        return s
    if lower.startswith("http://"):
        # Force https — Pinterest serves https everywhere; refuse downgrade.
        return "https://" + s[len("http://") :]
    if lower.startswith("//"):
        return "https:" + s
    # No scheme — promote.
    return "https://" + s


def _classify_one(url: str) -> PinInput:
    """Classify a single normalized URL. Returns NONE on host/path mismatch."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return PinInputNone()
    host = (parts.hostname or "").lower()
    if not _HOST_RE.match(host):
        return PinInputNone()

    path = parts.path or "/"

    # pin.it/* — always treated as PIN bucket
    if host == "pin.it":
        if _PIN_IT_PATH_RE.match(path):
            return PinInputPins(urls=(url,))
        return PinInputNone()

    # /pin/{digits}/ — PIN
    if _PIN_PATH_RE.match(path):
        return PinInputPins(urls=(url,))

    # /{user}/{board}/ — BOARD (must precede PROFILE check)
    if _BOARD_PATH_RE.match(path):
        return PinInputBoard(url=url)

    # /{user}/ — PROFILE
    if _PROFILE_PATH_RE.match(path):
        return PinInputProfile(url=url)

    return PinInputNone()


def classify_pinterest_input(text: str, *, max_pins: int = 20) -> PinInput:
    """Extract Pinterest URLs from free text and classify into 4-way taxonomy.

    Precedence: PIN > BOARD > PROFILE (REQ-ONBOARD-PINTEREST-002):

      - If ANY `/pin/...` URL is present → `Pins([all pins])`, board/profile dropped.
      - Elif ANY `/{u}/{b}/` URL is present → first `Board(...)`.
      - Elif ANY `/{u}/` URL is present → first `Profile(...)`.
      - Else `_None()`.

    Host validation is regex-after-`urlsplit` — never substring-on-raw-text.
    Spoofed hosts (`pinterest.com.evil.com`), wrong schemes (javascript:,
    http downgraded), and IDN homographs all classify NONE.

    Args:
        text: Free user-input text. May contain prose around URLs.
        max_pins: Cap on returned PIN urls (`truncated=True` if exceeded).

    @MX:SPEC: SPEC-ONBOARD-CARDS-001
    """
    if not text or not isinstance(text, str):
        return PinInputNone()

    pins: list[str] = []
    boards: list[str] = []
    profiles: list[str] = []

    for raw_match in _URL_TOKEN_RE.findall(text):
        normalized = _normalize_scheme(raw_match)
        if normalized is None:
            continue
        classified = _classify_one(normalized)
        if isinstance(classified, PinInputPins):
            pins.extend(classified.urls)
        elif isinstance(classified, PinInputBoard):
            boards.append(classified.url)
        elif isinstance(classified, PinInputProfile):
            profiles.append(classified.url)

    # Precedence cascade.
    if pins:
        truncated = len(pins) > max_pins
        return PinInputPins(urls=tuple(pins[:max_pins]), truncated=truncated)
    if boards:
        return PinInputBoard(url=boards[0])
    if profiles:
        return PinInputProfile(url=profiles[0])
    return PinInputNone()
