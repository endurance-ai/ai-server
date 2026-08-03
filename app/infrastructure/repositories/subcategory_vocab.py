"""Canonical subcategory vocabulary (v6 `p_subcategory` 정밀 필터).

2026-07-15 백엔드 카테고리 정규화와 동시 도입. `products.subcategory` 는
백엔드가 family 별 합의 vocabulary(hyphenated 소문자 단수형)로 채우는 중이며
(실 DB 확인: 66k/110k 행, knitwear 94% · tops 78% 채움), 이 모듈은 그
vocabulary 의 AI 서버 측 단일 소스다. Vision subcategory enum
(`vision_prompt.py` — SPEC-VISION-UNIFY-001 frozen mirror, 편집 금지) 및
LLM 자유형 단어를 canonical 토큰으로 정규화한다.

핵심 계약:
  - `search_products_v6` 의 `p_subcategory` 는 **EXACT 매치이고 어느 rung
    에서도 완화되지 않는다** (실물 함수 정의 확인, oid 338158). 값이
    vocabulary 밖이면 0결과 직행 → 이 모듈이 인식하지 못하는 입력은
    반드시 None 으로 떨어뜨려 필터를 끈다 (fail-open).
  - subcategory 는 family 보다 강한 신호다: Vision 은 hoodie 를 Outer,
    sweater 를 Top 소속으로 분류하지만 백엔드 vocabulary 는 hoodie→tops,
    sweater→knitwear 에 배치했다 (합의: 쿼리 측 alias 와 일치). 따라서
    subcategory 가 resolve 되면 그 family 로 `p_category` 를 보강한다.

`normalize_subcategory` 는 pure/total: 항상 `(subcategory|None, family|None)`
튜플을 반환하며 두 값은 함께 채워지거나 함께 None 이다.
"""

from __future__ import annotations

# canonical subcategory → canonical family. 백엔드 합의 vocabulary 전문
# (2026-07-13 회신 + 실 DB distinct 값 대조 완료). 새 토큰 추가 시 반드시
# 백엔드 vocabulary 와 동시 반영할 것 — 편측 추가는 EXACT 매치 특성상
# 0결과 필터가 된다.
SUBCATEGORY_FAMILY: dict[str, str] = {
    # tops
    "t-shirt": "tops",
    "shirt": "tops",
    "blouse": "tops",
    "polo": "tops",
    "hoodie": "tops",
    "sweatshirt": "tops",
    "tank-top": "tops",
    "crop-top": "tops",
    "henley": "tops",
    "camisole": "tops",
    # knitwear
    "sweater": "knitwear",
    "cardigan": "knitwear",
    "pullover": "knitwear",
    "knit-top": "knitwear",
    "turtleneck": "knitwear",
    # bottoms
    "jeans": "bottoms",
    "trousers": "bottoms",
    "chinos": "bottoms",
    "shorts": "bottoms",
    "skirt": "bottoms",
    "joggers": "bottoms",
    "cargo-pants": "bottoms",
    "wide-pants": "bottoms",
    "leggings": "bottoms",
    "sweatpants": "bottoms",
    # dresses
    "mini-dress": "dresses",
    "midi-dress": "dresses",
    "maxi-dress": "dresses",
    "shirt-dress": "dresses",
    "wrap-dress": "dresses",
    "slip-dress": "dresses",
    "knit-dress": "dresses",
    "jumpsuit": "dresses",
    # outerwear
    "overcoat": "outerwear",
    "trench-coat": "outerwear",
    "parka": "outerwear",
    "bomber": "outerwear",
    "blazer": "outerwear",
    "vest": "outerwear",
    "leather-jacket": "outerwear",
    "denim-jacket": "outerwear",
    "down-jacket": "outerwear",
    "windbreaker": "outerwear",
    "fleece": "outerwear",
    # shoes
    "sneakers": "shoes",
    "boots": "shoes",
    "loafers": "shoes",
    "derby": "shoes",
    "oxford": "shoes",
    "sandals": "shoes",
    "mules": "shoes",
    "heels": "shoes",
    "flats": "shoes",
    "slides": "shoes",
    "running-shoes": "shoes",
    # bags
    "tote": "bags",
    "crossbody": "bags",
    "backpack": "bags",
    "clutch": "bags",
    "shoulder-bag": "bags",
    "belt-bag": "bags",
    "messenger": "bags",
    "bucket-bag": "bags",
    # accessories
    "scarf": "accessories",
    "belt": "accessories",
    "watch": "accessories",
    "tie": "accessories",
    "gloves": "accessories",
    "socks": "accessories",
    # eyewear
    "sunglasses": "eyewear",
    "glasses": "eyewear",
    # jewelry
    "necklace": "jewelry",
    "bracelet": "jewelry",
    "ring": "jewelry",
    "earrings": "jewelry",
    # headwear
    "hat": "headwear",
    "cap": "headwear",
    "beanie": "headwear",
    "beret": "headwear",
    "bucket-hat": "headwear",
    # underwear / swimwear / activewear
    "briefs": "underwear",
    "bra": "underwear",
    "swimsuit": "swimwear",
    "bikini": "swimwear",
    "trunks": "swimwear",
    "tracksuit": "activewear",
    "sports-bra": "activewear",
    "athletic-shorts": "activewear",
}

# LLM 자유형 / Vision 변형 → canonical subcategory. 기계적 규칙(공백→하이픈,
# 복수형 -s 제거)으로 안 잡히는 동의어만 명시한다. 애매하면 넣지 않는다
# (예: "jacket" 은 bomber/blazer/denim-jacket 어느 것도 아니므로 subcategory
# 없이 family 게이트(outerwear)만 태운다 — category_family._VISION_ALIAS 담당).
_SUBCATEGORY_ALIAS: dict[str, str] = {
    "tee": "t-shirt",
    "tshirt": "t-shirt",
    "tank": "tank-top",
    "tanktop": "tank-top",
    "croptop": "crop-top",
    "hoody": "hoodie",
    "jumper": "sweater",
    "turtle-neck": "turtleneck",
    "pants": "trousers",
    "slacks": "trousers",
    "denim": "jeans",
    "denim-pants": "jeans",
    "jogger": "joggers",
    "sweatpant": "sweatpants",
    "legging": "leggings",
    "short": "shorts",
    "chino": "chinos",
    "sneaker": "sneakers",
    "trainers": "sneakers",
    "trainer": "sneakers",
    "boot": "boots",
    "loafer": "loafers",
    "heel": "heels",
    "sandal": "sandals",
    "mule": "mules",
    "flat": "flats",
    "slide": "slides",
    "coat": "overcoat",
    "trenchcoat": "trench-coat",
    "trench": "trench-coat",
    "puffer": "down-jacket",
    "minidress": "mini-dress",
    "mididress": "midi-dress",
    "maxidress": "maxi-dress",
    "handbag": "shoulder-bag",
    "earring": "earrings",
    "buckethat": "bucket-hat",
    "sock": "socks",
    "glove": "gloves",
    "sunglass": "sunglasses",
    "bikini-top": "bikini",
    "bikini-bottom": "bikini",
    "running-shoe": "running-shoes",
    "sports-shorts": "athletic-shorts",
}


def _lookup(token: str) -> str | None:
    """token 을 canonical subcategory 로 resolve (identity → alias 순)."""
    if token in SUBCATEGORY_FAMILY:
        return token
    return _SUBCATEGORY_ALIAS.get(token)


def normalize_subcategory(raw: str | None) -> tuple[str | None, str | None]:
    """임의 문자열 → `(canonical_subcategory, family)` — 실패 시 `(None, None)`.

    Resolution order (각 단계에서 identity → alias 조회):
      1. lower/strip 원형;
      2. 내부 공백·언더스코어 → 하이픈 (DB 미정규화 꼬리: "cargo pants");
      3. 말미 복수형 `-s` 제거 (예: "cardigans" → "cardigan").
         단, 제거 결과가 아니라 **원형이 canonical** 인 복수형 토큰
         (jeans/shorts/boots/…)은 1단계에서 이미 잡히므로 안전하다.

    인식 실패는 (None, None) — 호출부는 이때 p_subcategory 를 보내지 않는다
    (EXACT·무완화 필터라 모르는 값을 보내면 0결과가 되기 때문).
    """
    t = (raw or "").strip().lower()
    if not t:
        return (None, None)
    for candidate in (t, t.replace(" ", "-").replace("_", "-")):
        hit = _lookup(candidate)
        if hit is None and candidate.endswith("s"):
            hit = _lookup(candidate[:-1])
        if hit is not None:
            return (hit, SUBCATEGORY_FAMILY[hit])
    return (None, None)


__all__ = [
    "SUBCATEGORY_FAMILY",
    "normalize_subcategory",
]
