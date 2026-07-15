"""subcategory_vocab 유닛 테스트 (2026-07-15 백엔드 subcategory 정규화 연동).

v6 `p_subcategory` 는 EXACT·무완화 필터 — 이 vocab 이 인식하지 못하는 값은
반드시 (None, None) 으로 떨어져야 한다 (모르는 값 전달 = 0결과 필터).
"""

from __future__ import annotations

import pytest

from app.infrastructure.repositories.category_family import CANONICAL_FAMILIES
from app.infrastructure.repositories.subcategory_vocab import (
    SUBCATEGORY_FAMILY,
    normalize_subcategory,
)


def test_every_vocab_family_is_canonical():
    for sub, fam in SUBCATEGORY_FAMILY.items():
        assert fam in CANONICAL_FAMILIES, f"{sub}→{fam} not canonical"
        # sneakers family 는 상품 매핑 미사용 — vocab 이 가리키면 안 됨
        assert fam != "sneakers", f"{sub} must map to shoes, not sneakers"


@pytest.mark.parametrize(
    ("raw", "sub", "fam"),
    [
        # canonical identity
        ("hoodie", "hoodie", "tops"),
        ("sneakers", "sneakers", "shoes"),
        ("cardigan", "cardigan", "knitwear"),
        ("mini-dress", "mini-dress", "dresses"),
        ("cargo-pants", "cargo-pants", "bottoms"),
        # 합의 재배치: Vision 은 hoodie=Outer / sweater=Top 소속이지만
        # 백엔드 vocabulary 는 hoodie→tops, sweater→knitwear
        ("sweater", "sweater", "knitwear"),
        # alias (LLM 자유형)
        ("tee", "t-shirt", "tops"),
        ("tshirt", "t-shirt", "tops"),
        ("trainers", "sneakers", "shoes"),
        ("hoody", "hoodie", "tops"),
        ("puffer", "down-jacket", "outerwear"),
        # 케이스/공백/언더스코어 방어 (DB 미정규화 꼬리 형태)
        ("Cargo Pants", "cargo-pants", "bottoms"),
        ("T-Shirt", "t-shirt", "tops"),
        ("bucket hat", "bucket-hat", "headwear"),
        # 복수형 -s 제거
        ("cardigans", "cardigan", "knitwear"),
        ("scarves", None, None),  # 불규칙 복수형은 기계 규칙 밖 — fail-open
        ("belts", "belt", "accessories"),
        ("hats", "hat", "headwear"),
    ],
)
def test_normalize_recognized(raw, sub, fam):
    assert normalize_subcategory(raw) == (sub, fam)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "garbage",
        "knit",  # 애매(sweater vs knit-top) — 의도적 미등록
        "jacket",  # 애매(bomber/blazer/…) — family gate 만 (outerwear)
        "tops",  # family 단어는 subcategory 아님
        "apparel",
        "제품명 참조",
    ],
)
def test_normalize_unrecognized_fails_open(raw):
    assert normalize_subcategory(raw) == (None, None)


def test_plural_canonical_tokens_survive_identity():
    # 원형이 canonical 인 복수형 토큰이 -s 제거 규칙에 침식되지 않는지
    for token in ("jeans", "shorts", "boots", "heels", "flats", "slides", "gloves", "socks", "briefs", "trunks"):
        sub, fam = normalize_subcategory(token)
        assert sub == token, f"{token} must resolve to itself, got {sub}"
        assert fam == SUBCATEGORY_FAMILY[token]
