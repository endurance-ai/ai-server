"""Canonical category-family normalization (SPEC-SEARCH-V6-001 family-gate).

`search_products_v6` FILTER 2 is a canonical FAMILY gate. The RPC internally
does `lower(trim(p_category))` + a `category_canonical` lookup that resolves
the input to one of 20 canonical families (19 apparel + an `other`
catch-all). The gate engages ONLY when `p_category` is exactly one of those
20 lowercase tokens (every token is live as an identity row). ANY other
string — a Vision raw value, "Earrings", "악세사리", a brand style-node letter
— resolves to `other`, the family gate is skipped, and the RPC degrades
gracefully to cosine-only ranking (NOT broken; intentional).

DESIGN — call-site 2-layer map, NOT a prompt constraint:
  `app/channels/vision_prompt.py` is the SPEC-VISION-UNIFY-001 byte-verbatim
  mirror of kikoai/app `analyze.ts` with active drift-detection. It MUST NOT
  be edited. All 20-token normalization therefore happens HERE, at the search
  call site (sibling of `search_rpc_contract.py`, matching the repo pattern),
  and is plumbed into `SearchRepository.build_params`.

`to_canonical_family` is pure (no I/O), total (NEVER returns None — always
exactly one of the 20 lowercase tokens), and fully unit-testable.
"""

from __future__ import annotations

# The 20 canonical families (lowercase, verbatim). These are the ONLY allowed
# `p_category` tokens that engage the v6 family gate. `other` is the official
# catch-all — never invent a token outside this set.
CANONICAL_FAMILIES: frozenset[str] = frozenset(
    {
        "tops",
        "bottoms",
        "dresses",
        "outerwear",
        "knitwear",
        "shoes",
        "sneakers",
        "bags",
        "accessories",
        "eyewear",
        "jewelry",
        "headwear",
        "underwear",
        "swimwear",
        "activewear",
        "homeware",
        "fragrance",
        "beauty",
        "lifestyle",
        "other",
    }
)

# 2026-07-15 백엔드 정규화 반영: products.category 는 이제 14 family + other
# 로 정규화 완료 (실 DB 확인 — sneakers 카테고리 상품 0행). `sneakers` family
# 로 게이트를 걸면 rung 2 count=0 → rung 3 로 떨어져 family gate 가 통째로
# 무력화되므로, sneakers 로 resolve 되는 모든 입력은 `shoes` 로 리맵한다.
# 스니커즈 정밀도는 subcategory 레벨(`p_subcategory='sneakers'`, DB 2.3k행)
# 에서 해결된다 — subcategory_vocab.py 참조.
_FAMILY_REMAP: dict[str, str] = {
    "sneakers": "shoes",
}

# Bot Vision 7-enum (vision_prompt.py:150 — `Outer/Top/Bottom/Shoes/Bag/Dress/
# Accessories`) lowercased → canonical family. Plus a SMALL, conservative set
# of obvious singular/plural/synonym defensives whose target is one of the 20.
# When unsure → DO NOT add an entry; let it fall through to `other`.
_VISION_ALIAS: dict[str, str] = {
    # Vision 7-enum (the primary, authoritative mapping)
    "outer": "outerwear",
    "top": "tops",
    "bottom": "bottoms",
    "shoes": "shoes",
    "bag": "bags",
    "dress": "dresses",
    "accessories": "accessories",
    # 260611 — LLM `category` arg is free-form English (from prompt: "pass a
    # concise ENGLISH text_query / category" — search_products.py). Without
    # these aliases nearly every search resolves to `other` and the family
    # gate is skipped, so ranking degrades to cosine-only and a "모자" query
    # can match sweaters. Each entry targets exactly one of the 20 canonical
    # families. When unsure (e.g. ambiguous "scarf" → could be accessories
    # OR knitwear) leave it out and let `other` engage.
    # tops
    "shirt": "tops",
    "shirts": "tops",
    "tshirt": "tops",
    "t-shirt": "tops",
    "tee": "tops",
    "tees": "tops",
    "sleeveless": "tops",
    "tank": "tops",
    "tank-top": "tops",
    "blouse": "tops",
    "hoodie": "tops",
    "hoody": "tops",
    "sweatshirt": "tops",
    "polo": "tops",
    "henley": "tops",
    "crop-top": "tops",
    "croptop": "tops",
    # knitwear
    "sweater": "knitwear",
    "sweaters": "knitwear",
    "knit": "knitwear",
    "cardigan": "knitwear",
    "pullover": "knitwear",
    "jumper": "knitwear",
    # bottoms
    "skirt": "bottoms",
    "skirts": "bottoms",
    "pants": "bottoms",
    "pant": "bottoms",
    "jeans": "bottoms",
    "denim": "bottoms",
    "shorts": "bottoms",
    "trousers": "bottoms",
    "leggings": "bottoms",
    "slacks": "bottoms",
    "skort": "bottoms",
    # outerwear
    "jacket": "outerwear",
    "blazer": "outerwear",
    "coat": "outerwear",
    "parka": "outerwear",
    "trench": "outerwear",
    "trenchcoat": "outerwear",
    "puffer": "outerwear",
    "vest": "outerwear",
    "windbreaker": "outerwear",
    # shoes (2026-07-15: sneakers family 는 상품 매핑에 미사용 — shoes 로 통합)
    "sneaker": "shoes",
    "sneakers": "shoes",
    "trainers": "shoes",
    "boots": "shoes",
    "boot": "shoes",
    "loafers": "shoes",
    "loafer": "shoes",
    "heels": "shoes",
    "heel": "shoes",
    "sandals": "shoes",
    "sandal": "shoes",
    "mules": "shoes",
    "flats": "shoes",
    # bags
    "bags": "bags",
    "handbag": "bags",
    "tote": "bags",
    "backpack": "bags",
    "clutch": "bags",
    "crossbody": "bags",
    # headwear
    "jewellery": "jewelry",
    "glasses": "eyewear",
    "sunglasses": "eyewear",
    "hat": "headwear",
    "hats": "headwear",
    "cap": "headwear",
    "caps": "headwear",
    "beanie": "headwear",
    "beret": "headwear",
    "bucket-hat": "headwear",
    "buckethat": "headwear",
    # dresses (alias for plural / variants)
    "dresses": "dresses",
    "mini-dress": "dresses",
    "minidress": "dresses",
    "maxi-dress": "dresses",
    "maxidress": "dresses",
    "midi-dress": "dresses",
    "mididress": "dresses",
    "jumpsuit": "dresses",
    # swimwear / activewear
    "swimsuit": "swimwear",
    "bikini": "swimwear",
    "trunks": "swimwear",
    "tracksuit": "activewear",
    "sportswear": "activewear",
    "athletic": "activewear",
}


def to_canonical_family(raw: str | None) -> str:
    """Normalize an arbitrary category string to a canonical v6 family token.

    Resolution order:
      1. identity passthrough — `t in CANONICAL_FAMILIES` → return `t`
         (supports the /recommend path where the app may already send a
         20-token, and any future Vision canonical output);
      2. Vision-alias map — `t in _VISION_ALIAS` → return the mapped family;
      3. else → `"other"` (the v6 catch-all → family gate intentionally
         skipped → graceful cosine-only degrade).
    The resolved token then passes `_FAMILY_REMAP` (sneakers→shoes — the
    sneakers family has zero product rows since the 2026-07 backend
    normalization, so gating on it would silently kill the family gate).

    Always returns exactly ONE lowercase token from `CANONICAL_FAMILIES`;
    NEVER returns None (the v6 contract requires `p_category` to be exactly
    one lowercase token).
    """
    t = (raw or "").strip().lower()
    if t in CANONICAL_FAMILIES:
        return _FAMILY_REMAP.get(t, t)
    if t in _VISION_ALIAS:
        t = _VISION_ALIAS[t]
        return _FAMILY_REMAP.get(t, t)
    return "other"


# 2026-09-25 — 맨-브랜드 라우터("자라 아우터", "팔로마 울 가방")가 남은 품목
# 한 단어를 family 로 걸기 위한 한글 품목어 → family. 품목어 한 토큰만 받는다
# (문장 파싱 아님). 정확 일치 먼저, 없으면 접미 일치('보스턴백'→백, '후드집업'
# →집업). 모호한 단어(니트=소재/품목, 데님=소재/바지)는 품목 위치에 단독으로
# 올 때 품목 의미가 우세하므로 포함한다. 확신 없으면 넣지 말 것 → None.
_KO_GARMENT_FAMILY: dict[str, str] = {
    # outerwear
    "아우터": "outerwear",
    "자켓": "outerwear",
    "재킷": "outerwear",
    "코트": "outerwear",
    "점퍼": "outerwear",
    "잠바": "outerwear",
    "패딩": "outerwear",
    "블루종": "outerwear",
    "블레이저": "outerwear",
    "트렌치": "outerwear",
    "트렌치코트": "outerwear",
    "바람막이": "outerwear",
    "야상": "outerwear",
    "무스탕": "outerwear",
    "조끼": "outerwear",
    # tops
    "상의": "tops",
    "티": "tops",
    "티셔츠": "tops",
    "반팔": "tops",
    "긴팔": "tops",
    "롱슬리브": "tops",
    "셔츠": "tops",
    "남방": "tops",
    "블라우스": "tops",
    "탑": "tops",
    "나시": "tops",
    "민소매": "tops",
    "후드": "tops",
    "후디": "tops",
    "후드티": "tops",
    "후드집업": "tops",
    "집업": "tops",
    "맨투맨": "tops",
    "스웻셔츠": "tops",
    # knitwear
    "니트": "knitwear",
    "스웨터": "knitwear",
    "가디건": "knitwear",
    # bottoms
    "하의": "bottoms",
    "바지": "bottoms",
    "팬츠": "bottoms",
    "청바지": "bottoms",
    "데님": "bottoms",
    "진": "bottoms",
    "슬랙스": "bottoms",
    "조거": "bottoms",
    "반바지": "bottoms",
    "쇼츠": "bottoms",
    "치마": "bottoms",
    "스커트": "bottoms",
    "레깅스": "bottoms",
    # dresses
    "원피스": "dresses",
    "드레스": "dresses",
    # shoes
    "신발": "shoes",
    "구두": "shoes",
    "운동화": "shoes",
    "스니커즈": "shoes",
    "부츠": "shoes",
    "로퍼": "shoes",
    "샌들": "shoes",
    "슬리퍼": "shoes",
    "힐": "shoes",
    "뮬": "shoes",
    # bags
    "가방": "bags",
    "백": "bags",
    "백팩": "bags",
    "토트": "bags",
    "크로스백": "bags",
    "숄더백": "bags",
    "클러치": "bags",
    # headwear / jewelry / eyewear
    "모자": "headwear",
    "캡": "headwear",
    "비니": "headwear",
    "버킷햇": "headwear",
    "주얼리": "jewelry",
    "귀걸이": "jewelry",
    "귀고리": "jewelry",
    "목걸이": "jewelry",
    "반지": "jewelry",
    "팔찌": "jewelry",
    "안경": "eyewear",
    "선글라스": "eyewear",
    # accessories
    "지갑": "accessories",
    "벨트": "accessories",
    "머플러": "accessories",
    "스카프": "accessories",
    "장갑": "accessories",
    "양말": "accessories",
}

# 접미 일치 규칙(길이 내림차순으로 시도). 합성 품목어용 — '보스턴백', '미니백',
# '레더자켓', '울코트', '와이드팬츠'. 짧은 접미('백')는 2음절 이상 단어에만 적용.
_KO_GARMENT_SUFFIXES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            ("백", "bags"),
            ("가방", "bags"),
            ("자켓", "outerwear"),
            ("재킷", "outerwear"),
            ("코트", "outerwear"),
            ("점퍼", "outerwear"),
            ("패딩", "outerwear"),
            ("블루종", "outerwear"),
            ("셔츠", "tops"),
            ("티셔츠", "tops"),
            ("블라우스", "tops"),
            ("집업", "tops"),
            ("후드", "tops"),
            ("후디", "tops"),
            ("니트", "knitwear"),
            ("가디건", "knitwear"),
            ("팬츠", "bottoms"),
            ("바지", "bottoms"),
            ("스커트", "bottoms"),
            ("슬랙스", "bottoms"),
            ("원피스", "dresses"),
            ("드레스", "dresses"),
            ("부츠", "shoes"),
            ("스니커즈", "shoes"),
            ("로퍼", "shoes"),
            ("샌들", "shoes"),
        ),
        key=lambda kv: -len(kv[0]),
    )
)


def garment_family(token: str | None) -> str | None:
    """품목어 한 토큰 → canonical family, 품목어가 아니면 None.

    한글은 `_KO_GARMENT_FAMILY` 정확 일치 → 접미 일치 순. 영문은
    `to_canonical_family` 재사용(미인식 `other` 는 None 으로 돌려 "품목 아님"을
    표현). 맨-브랜드 라우터가 "브랜드 + 품목 1개" 요청의 품목을 family 필터로
    걸 때 쓴다 — 확신 없는 입력은 None(라우터는 기존 동작 유지).
    """
    t = (token or "").strip().lower()
    if not t:
        return None
    if t in _KO_GARMENT_FAMILY:
        return _KO_GARMENT_FAMILY[t]
    if any("가" <= ch <= "힣" for ch in t):
        for suffix, fam in _KO_GARMENT_SUFFIXES:
            if t.endswith(suffix) and len(t) > len(suffix):
                return fam
        return None
    fam = to_canonical_family(t)
    return None if fam == "other" else fam


# 품목어 → 영문 검색어 힌트. FashionSigLIP 텍스트 인코더는 영어 기준이라 한글
# 품목어('보스턴백')를 그대로 임베딩하면 순위가 흐트러진다. family 필터만으론
# '가방 전체'라서 보스턴백을 앞으로 끌어올 신호가 필요하다(9/11 마르지엘라
# 보스턴백 세션). 정확 일치 → 접미 일치 순. 없으면 None(호출부는 family 로 폴백).
_KO_GARMENT_EN: dict[str, str] = {
    "보스턴백": "boston duffle bag",
    "더플백": "duffle bag",
    "토트백": "tote bag",
    "토트": "tote bag",
    "크로스백": "crossbody bag",
    "숄더백": "shoulder bag",
    "백팩": "backpack",
    "클러치": "clutch bag",
    "호보백": "hobo bag",
    "버킷백": "bucket bag",
    "미니백": "mini bag",
    "메신저백": "messenger bag",
    "가방": "bag",
    "백": "bag",
    "아우터": "outerwear jacket coat",
    "자켓": "jacket",
    "재킷": "jacket",
    "코트": "coat",
    "트렌치코트": "trench coat",
    "패딩": "padded puffer jacket",
    "블루종": "blouson jacket",
    "블레이저": "blazer",
    "바람막이": "windbreaker",
    "셔츠": "shirt",
    "블라우스": "blouse",
    "티셔츠": "t-shirt",
    "후드": "hoodie",
    "후디": "hoodie",
    "후드집업": "zip-up hoodie",
    "맨투맨": "sweatshirt",
    "니트": "knit sweater",
    "가디건": "cardigan",
    "바지": "pants",
    "팬츠": "pants",
    "청바지": "jeans",
    "슬랙스": "slacks trousers",
    "스커트": "skirt",
    "치마": "skirt",
    "원피스": "dress",
    "드레스": "dress",
    "스니커즈": "sneakers",
    "운동화": "sneakers",
    "부츠": "boots",
    "로퍼": "loafers",
    "구두": "leather shoes",
    "샌들": "sandals",
}


def garment_query_en(token: str | None) -> str | None:
    """품목어 한 토큰 → 영문 검색어 힌트(없으면 None). 영문 입력은 그대로 쓴다."""
    t = (token or "").strip().lower()
    if not t:
        return None
    if t in _KO_GARMENT_EN:
        return _KO_GARMENT_EN[t]
    if not any("가" <= ch <= "힣" for ch in t):
        return t
    for key in sorted(_KO_GARMENT_EN, key=len, reverse=True):
        if len(key) >= 2 and t.endswith(key) and len(t) > len(key):
            return _KO_GARMENT_EN[key]
    return None


# 카탈로그의 `bags` family 에는 지갑·카드지갑이 섞여 있다(서브카테고리도 clutch/
# crossbody 로 오분류 — 마르지엘라 남성 'bags' 재고 전부가 지갑류). 가방을 찾는
# 요청에선 이 이름들을 뺀다. 사용자가 지갑 자체를 찾는 경우엔 쓰지 않는다.
SMALL_LEATHER_GOODS_TERMS: tuple[str, ...] = (
    "wallet",
    "card holder",
    "cardholder",
    "card case",
    "coin purse",
    "passport",
    "지갑",
    "카드",
)


__all__ = [
    "CANONICAL_FAMILIES",
    "SMALL_LEATHER_GOODS_TERMS",
    "garment_family",
    "garment_query_en",
    "to_canonical_family",
]
