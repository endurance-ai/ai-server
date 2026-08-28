"""Shared keyword helpers — used by `search_products` and `refine_search`.

Both tools accept `boost_keywords` / `exclude_keywords` from the LLM and need
to (a) defensively cast them to a clean list[str] and (b) join boost tokens
into the text_query without duplicating tokens already present (the B15
fix: chained refines no longer accumulate "roomy roomy roomy").

Lives in its own module so `search_products` and `refine_search` can both
import without creating a cycle (refine_search already imports from
search_products).
"""

from __future__ import annotations

import re

# LLM(특히 kimi-k2.5)이 boost_keywords 에 실제 키워드 대신 스키마 타입 표기를
# 값으로 뱉는 malform 방어. 실트레이스(2026-08-24): boost_keywords="list[3]" →
# 임베딩 쿼리에 'list[3]' 토큰 오염. 명백한 placeholder 만 화이트리스트로 걷어낸다
# (list[N]/str/dict 등 타입어, <keyword> 꺾쇠, ... 생략표시). 실 키워드는 안 지운다.
_PLACEHOLDER_RE = re.compile(
    r"^(?:"
    r"(?:list|array|tuple|set|str|string|int|integer|float|number|bool|boolean|"
    r"dict|object|any|none|null|optional|e\.?g\.?)(?:\[.*\])?"
    r"|<.+>"
    r"|\.{2,}"
    r"|keywords?\d*"
    r")$",
    re.IGNORECASE,
)


def _is_placeholder(token: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(token.strip()))


# color_family 캐노니컬 어휘 + 흔한 표기 변형. refine_search 의 color_swap 이
# base_query 에서 이전 색 단어를 걷어낼 때 쓴다("black cropped hoodie" 에
# color=pink 를 줘도 임베딩이 검정을 당기던 버그). pick_item._COLOR_FAMILY_KO
# 와 동일 집합 + light/dark 수식어. 화이트리스트라 비색상 토큰은 절대 안 지운다.
COLOR_WORDS: frozenset[str] = frozenset(
    {
        "black",
        "white",
        "grey",
        "gray",
        "beige",
        "brown",
        "navy",
        "blue",
        "green",
        "red",
        "pink",
        "purple",
        "yellow",
        "orange",
        "cream",
        "khaki",
        "ivory",
        "burgundy",
        "tan",
        "olive",
        "mint",
        "lavender",
        "coral",
        "maroon",
        "teal",
        "gold",
        "silver",
        "charcoal",
        "light",
        "dark",
        "neon",
    }
)


def strip_color_tokens(query: str) -> str:
    """`query` 에서 알려진 색 단어(COLOR_WORDS)만 제거하고 나머지는 보존.

    color_swap refine 에서 이전 색이 임베딩 쿼리에 눌러앉아 새 색과 충돌하는
    걸 막는다. 화이트리스트 방식이라 색이 아닌 토큰은 안 건드린다.
    """
    kept = [t for t in (query or "").split() if t.lower() not in COLOR_WORDS]
    return " ".join(kept).strip()


def as_keyword_list(v: object) -> list[str]:
    """Defensive cast for `boost_keywords` / `exclude_keywords` args.

    `validate_args` already rejects non-list inputs at the agent boundary,
    but if anything slips through (test monkeypatch, future tool added with
    a different shape), naive `list(some_string)` explodes a single keyword
    string into per-character tokens (["t","-","s","h","i","r","t"]) that
    then contaminate the embedded query. This belt-and-suspenders cast
    keeps the embed input well-formed regardless of upstream validation
    state.

    Mapping:
      None / empty / whitespace-only → []
      single string                  → [string.strip()]
      list/tuple                     → [str(x) for each truthy x]
      anything else                  → []
    """
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        return [s] if s and not _is_placeholder(s) else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x and not _is_placeholder(str(x))]
    return []


def dedup_join(base_query: str, boost: list[str]) -> str:
    """Join `base_query` + `boost` keywords with case-insensitive token
    dedup. Order from base_query is preserved (it's the canonical product
    query); new boost tokens are appended only when not already present.
    Whitespace-normalised.

    Examples:
      base="wide jeans women roomy", boost=["roomy"]
        → "wide jeans women roomy"           (no dup)
      base="black blazer", boost=["cropped", "side-button"]
        → "black blazer cropped side-button"
      base="", boost=["red", "dress"]
        → "red dress"
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in (base_query or "").split():
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    for kw in boost:
        for tok in str(kw).split():
            key = tok.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(tok)
    return " ".join(out).strip()
