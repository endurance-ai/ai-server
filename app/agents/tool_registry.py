"""SPEC-AGENT-V2-REACT / REQ-AGENT-TOOL-CATALOG-001 — 7-tool registry.

Single source of truth for the LLM-callable tools. Each entry:
- `args` TypedDict — the schema the LLM must satisfy
- `result` TypedDict — the shape returned to the loop (LLM-consumable summary)
- `dispatch_fn_path` — dotted import path to the async dispatcher (lazy import)
- `description` — human-readable / LLM-readable doc string
- `langfuse_span_tag` — span name for observability
- `side_effect_doc` — operator-facing note on side effects

Schema enforcement: `validate_args(name, args)` runs at dispatch time
(REQ-AGENT-TOOL-DISPATCH-001) using TypedDict's `__required_keys__` /
`__optional_keys__` introspection.

@MX:NOTE: [AUTO] SPEC-AGENT-V2-REACT — 7-tool dispatch table single source of truth
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

__all__ = [
    "REGISTRY",
    "ToolMetadata",
    "ToolResult",
    # Tool args
    "AnalyzeImageArgs",
    "SearchProductsArgs",
    "RefineSearchArgs",
    "UpdateTasteArgs",
    "AskUserClarificationArgs",
    "GetRecentHistoryArgs",
    "WebSearchArgs",
    "RespondArgs",
    # SPEC-AGENT-V3-REACT Gap3 — 8th tool (flag-gated registration)
    "SuggestNextStepArgs",
    # Tool results
    "AnalyzeImageResult",
    "SearchProductsResult",
    "RefineSearchResult",
    "UpdateTasteResult",
    "AskUserClarificationResult",
    "GetRecentHistoryResult",
    "WebSearchResult",
    "RespondResult",
    "SuggestNextStepResult",
    # Helpers
    "validate_args",
    "TOOL_NAMES",
]


# ── Common shapes ──────────────────────────────────────────────────────────


class ToolResult(TypedDict, total=False):
    """Generic result envelope. Per-tool TypedDicts narrow this."""

    ok: bool
    error: str | None
    result_summary: dict[str, Any]


# ── Args TypedDicts (7 tools) ──────────────────────────────────────────────


class AnalyzeImageArgs(TypedDict, total=False):
    # P1-2/4 (SPEC-AGENT-V2-REACT review): `image_url` is intentionally NOT a
    # field. Mirrors the SearchProductsArgs hardening — the tool sources the
    # resolved pin/og:image URL ONLY from ctx (populated pre-agent by the
    # resolve_image node). Exposing it let a prompt-injected LLM fabricate a
    # crafted SSRF URL; with no field, validate_args auto-rejects any supplied
    # `image_url` as unknown_keys.
    pass


class SearchProductsArgs(TypedDict, total=False):
    # NOTE (SPEC-AGENT-V2-REACT root-bug fix): `image_url` is intentionally
    # NOT a field here. The tool sources imagery internally from the resolved
    # session/ctx state. Exposing it let the LLM fabricate placeholder URLs
    # that Modal embedded → vector search missed the whole catalog → 0 results.
    text_query: str
    style_node_primary: str | None
    color_family: str | None
    fit: str | None
    # 2026-08-31 — 명시 속성 필터(rerank "우와 비슷하다" 디테일 축). 풀커버리지 223k.
    #   material: 소재(linen/cotton/knit/wool/denim/leather/suede/corduroy/…)
    #   pattern: 무늬(striped/checked/floral/dot/camo/graphic/… ; solid 은 제외)
    #   neckline: 넥라인(v-neck/crew-neck/turtleneck/off-shoulder/…)
    material: str | None
    pattern: str | None
    neckline: str | None
    # 2026-08-31 — v2.6 enrichment(product_features_v26) 축. 고커버리지.
    #   length: 기장(regular/full/crop/knee/midi/mini/maxi/long/ankle/micro)
    #   sleeve_length: 소매(long/short/sleeveless/three_quarter)
    #   leg_shape: 다리통(wide/straight/flare/skinny/tapered/barrel)
    length: str | None
    sleeve_length: str | None
    leg_shape: str | None
    # 2026-09-01 — v2.6 스타일 무드 태그(product_features_v26.final_tags). 사용자가
    #   무드/트렌드를 명시할 때만. 후보풀 하드필터(그 무드 상품만). 27개 폐쇄값 중 하나.
    mood: str | None
    # 2026-09-01 — v2.6 스타일 디테일축(product_features_v26.attr). rerank 소프트 가산.
    #   surface: 소재감(matte/glossy/metallic/sheer/sequin/coated)
    #   texture: 질감(ribbed/cable/distressed/crochet/lace/corduroy/washed/…)
    #   design_details: 디테일(cutout/slit/wrap/corset/ruffle/…)  ※단수 토큰
    surface: str | None
    texture: str | None
    design_details: str | None
    # 2026-09-01 — v2.6 비어패럴 조건부축(product_features_v26.attr). 해당 카테고리에서만.
    #   신발: heel_type(flat/block/stiletto/kitten/wedge/platform) · heel_height(flat/low/mid/high)
    #        · shaft(none/ankle/mid_calf/knee/thigh) · shoe_toe(round/pointed/square/almond/open)
    #   가방: bag_size(micro/mini/small/medium/large) · bag_structure(structured/semi/slouchy)
    #   안경: frame_shape(round/square/cat_eye/aviator/rectangle/oval/shield)
    #   주얼리: metal_tone(gold/silver/rose/mixed/black)
    heel_type: str | None
    heel_height: str | None
    shaft: str | None
    shoe_toe: str | None
    bag_size: str | None
    bag_structure: str | None
    frame_shape: str | None
    metal_tone: str | None
    # 2026-09-02 — v2.6 wash(데님 워싱: raw/light/medium/dark/acid/bleached) ·
    #   graphics(none/logo/graphic/text/allover — 프린트/로고 유형).
    wash: str | None
    graphics: str | None
    # 2026-07-16 — garment 단어 (예: "hoodie", "sneakers"). dispatch 가
    # vision_category 부재 시(순수 텍스트 턴) family gate + p_subcategory
    # 파생에 사용. 종전엔 스키마에 없어 unknown_keys 로 거부되던 배선.
    category: str | None
    # 2026-07-16 — 사용자가 특정 브랜드를 지정한 경우 ("아크네 가디건").
    # brand_node_cache 로 canonical 명 resolve → p_brand_names EXACT 필터.
    brand: str | None
    # 2026-08-19 — 특정 상품/모델을 지목한 경우 상품명 매칭어. 상품명에 나올
    # 법한 고유 서술어/모델 토큰만 (예: '2021M', 'trompe l’oeil', 'museum').
    # products.name 을 word-trigram 매칭해 그 상품을 상단으로 부스트한다.
    name_query: str | None
    # 2026-07-16 — 상황/TPO 쿼리("결혼식 하객룩")를 구성 아이템으로 확장.
    # 특정 옷 이름이 없는 상황 쿼리에서만 2~3개 아이템 쿼리를 채운다.
    # dispatch 가 각각 병렬 검색 후 인터리브 병합. gender 는 시스템이
    # 각 쿼리에 적용하므로 순수 아이템 쿼리만 (gender 단어 불필요).
    sub_queries: list[str]
    min_price: float | None
    max_price: float | None
    exclude_keywords: list[str]
    # 2026-08-30 (D2) — 첫 턴에 특정 브랜드 배제("자라 빼고 검정 재킷"). refine_search
    # 의 exclude_brands 와 대칭. dispatch 가 candidate.brand 를 client-side 매칭해 드롭.
    exclude_brands: list[str]
    boost_keywords: list[str]
    # NOTE (2026-05-20): `top_k` removed from LLM schema — Haiku 가 카드 캐러셀
    # 크기(5)를 따라하며 페이지네이션을 죽였음. dispatch 기본값 15 유지.


class RefineSearchArgs(TypedDict, total=False):
    # action enum mirrors v1 critique vocabulary
    action: Literal["broaden", "refine", "exclude", "cheaper", "color_swap"]
    exclude_brands: list[str]
    exclude_keywords: list[str]
    boost_keywords: list[str]
    color: str | None
    max_price: float | None
    min_price: float | None
    drop_min_price: bool
    # 2026-09-01 — v2.6 스타일 무드 태그(final_tags). 직전 결과를 특정 무드로 좁히는
    #   delta refine ("리조트st로", "그런지 느낌으로"). search_products.mood 와 동일 27 폐쇄값.
    mood: str | None
    # SPEC-SEARCH-V6-STYLE-WIRING text-only follow-up: optional 1-letter style
    # node override. Same digest is appended to the refine_search tool
    # description by style_node.warm_cache().
    style_node_primary: str | None


class UpdateTasteArgs(TypedDict, total=False):
    # source enum matches TasteSource (catalog #18)
    source: Literal["click", "onboard", "pinterest", "critique", "free_text", "no_click", "re_query"]
    brand_likes: list[str]
    brand_dislikes: list[str]
    keyword_likes: list[str]
    keyword_dislikes: list[str]


class AskUserClarificationArgs(TypedDict, total=False):
    axis: Literal["category_pick", "formality", "fit", "occasion", "subcategory_disambiguation", "generic_fallback"]
    options: list[str]
    prompt: str


class GetRecentHistoryArgs(TypedDict, total=False):
    n: int  # ≤ 20
    event_types: list[str]  # optional filter


class WebSearchArgs(TypedDict, total=False):
    query: str  # what to look up on the web (English or Korean)


class SuggestNextStepArgs(TypedDict, total=False):
    # SPEC-AGENT-V3-REACT Gap3 — proactive follow-up options card.
    kind: Literal["similar", "fit_change", "different_mood", "broaden", "generic"]
    options: list[str]
    prompt: str


class RespondArgs(TypedDict, total=False):
    # NOTE (SPEC-AGENT-V2-REACT cards-spam fix): `cards` is intentionally NOT a
    # field. The LLM (esp. nova-lite) cannot serialize search candidates and
    # char-exploded a markdown string into a per-character list → 1-char
    # channel sends + empty-string rejections. Cards are now sourced internally by
    # the respond tool from the turn's last search (`sess.last_results`).
    text: str


# ── Result TypedDicts (7 tools) ────────────────────────────────────────────


class AnalyzeImageResult(TypedDict, total=False):
    ok: bool
    error: str | None
    style_node_primary: str | None
    mood: list[str]
    palette: list[str]
    items_count: int
    subcategory: str | None
    fit: str | None
    color_family: str | None
    search_query: str | None


class SearchProductsResult(TypedDict, total=False):
    ok: bool
    error: str | None
    candidates_count: int
    top_candidates: list[dict[str, Any]]  # capped to 5 for LLM context
    # 요청 속성(색 등)이 재고 부족으로 완화됐을 때의 정직 안내 신호. 에이전트가
    # 사용자에게 "요청 색이 거의 없어 유사상품으로 채웠다"고 전달하도록 respond
    # 에 반영. None/부재 = 완화 없음(정상 색 매치).
    notice: str | None
    # 결과셋 속성 분포 요약(주력 종류/핏/소재/색/가격대/브랜드믹스). respond 가
    # 이걸 근거로 "대부분 미디에 린넨" 처럼 구체적으로 묘사(데이드림 벤치마크).
    # 지어내기 방지 — 모델은 digest 에 있는 속성만 말해야 함.
    digest: dict[str, Any] | None


class RefineSearchResult(TypedDict, total=False):
    ok: bool
    error: str | None
    candidates_count: int
    top_candidates: list[dict[str, Any]]
    notice: str | None
    digest: dict[str, Any] | None


class UpdateTasteResult(TypedDict, total=False):
    ok: bool
    error: str | None
    applied: bool


class AskUserClarificationResult(TypedDict, total=False):
    ok: bool
    error: str | None
    card_sent: bool
    axis: str


class GetRecentHistoryResult(TypedDict, total=False):
    ok: bool
    error: str | None
    events: list[dict[str, Any]]


class WebSearchResult(TypedDict, total=False):
    ok: bool
    error: str | None
    answer: str | None  # provider's synthesized answer (may be None)
    results: list[dict[str, Any]]  # [{title, url, content}] snippets


class RespondResult(TypedDict, total=False):
    ok: bool
    error: str | None
    text_sent: bool
    cards_sent: int


class SuggestNextStepResult(TypedDict, total=False):
    ok: bool
    error: str | None
    card_sent: bool
    kind: str


# ── Metadata + REGISTRY ────────────────────────────────────────────────────


class ToolMetadata(TypedDict):
    name: str
    description: str
    args_typeddict: type
    result_typeddict: type
    dispatch_fn_path: str  # dotted import path: "app.agents.tools.X:dispatch"
    langfuse_span_tag: str
    side_effect_doc: str
    terminates_loop: bool  # True only for `respond`


REGISTRY: dict[str, ToolMetadata] = {
    "analyze_image": {
        "name": "analyze_image",
        "description": (
            "Analyze the user's fashion image and return structured style info. "
            "Use when the user provides a photo or Pinterest link and Vision hasn't run. "
            "Takes NO arguments — do NOT provide an image_url; the tool sources the "
            "resolved image internally from session state."
        ),
        "args_typeddict": AnalyzeImageArgs,
        "result_typeddict": AnalyzeImageResult,
        "dispatch_fn_path": "app.agents.tools.analyze_image:dispatch",
        "langfuse_span_tag": "tool.analyze_image",
        "side_effect_doc": "Calls Vision LLM (LiteLLM). SSRF-guarded URL validation.",
        "terminates_loop": False,
    },
    "search_products": {
        "name": "search_products",
        "description": (
            "Search the 200k+ product catalog. Provide `text_query` plus optional "
            "filters (category, brand, style_node_primary, color_family, fit, price). Do "
            "NOT provide an image_url — the tool handles imagery internally "
            "from session state. Returns top candidates with brand/title/price.\n"
            "\n"
            "  - `category`: the garment word of the query as ONE singular English "
            "token (same word as in text_query — e.g. 'hoodie', 'sneakers', "
            "'midi-dress', 'cargo-pants'). ALWAYS set it when the user asks for a "
            "specific garment type; it powers a precise catalog filter.\n"
            "  - `brand`: ONLY when the user explicitly names a brand "
            "(e.g. '아크네 가디건' → brand='acne studios'). Prefer the English "
            "brand name; if you are not sure of the English spelling (Korean "
            "indie labels, abbreviations like 'paf'), pass the brand exactly as "
            "the user wrote it — the resolver matches Korean, English, and "
            "acronyms. NEVER invent a brand the user didn't mention. When the "
            "user explicitly LABELS a word a brand — 'X 브랜드', '브랜드 X', "
            "'X 브랜드 제품/옷' — set brand=X EVEN IF X is also a common "
            "material/color word. e.g. '스웨이드 브랜드 제품 추천' → brand='스웨이드' "
            "(the brand SUADE), NOT color_family/material 'suede'.\n"
            "  - `name_query`: ONLY when the user names a SPECIFIC product / model / "
            "line (e.g. '아크네 2021M 진', 'the museum shirt', 'trompe l’oeil 진'). Put "
            "the distinctive descriptor here in ENGLISH ('2021M', 'museum', 'trompe "
            "l’oeil'); it word-matches products.name and boosts that exact item to the "
            "top. Leave empty for generic garment requests ('바지 추천').\n"
            "  - `max_price` / `min_price`: budget bounds as KRW integer 원 (no symbol). "
            "SET on a FRESH request that names a budget — '10만원 이하 후드' → "
            "max_price=100000; '5만원 이상' → min_price=50000; 'under $100' → "
            "max_price=130000. (For 'cheaper'/'더 저렴한' that ADJUSTS the CURRENT "
            "results, use refine_search instead — see below.)\n"
            "  - `exclude_brands`: brands to EXCLUDE on a FRESH search (e.g. '자라 빼고 "
            "검정 재킷' → exclude_brands=['Zara'], text_query='black jacket'). Use when a "
            "NEW request names a brand to AVOID. Prefer the English brand name. (To exclude "
            "from results ALREADY shown, use refine_search action='exclude'.)\n"
            "  - `material` / `pattern` / `neckline`: query DETAIL attributes, set ONLY when "
            "the user names them. ENGLISH lowercase (hyphen-join multiword). material "
            "('린넨'→'linen', 코튼/니트/울/데님/가죽/스웨이드/코듀로이), pattern ('스트라이프'→"
            "'striped', 체크/플로럴/도트/카모/그래픽 — NOT solid), neckline ('브이넥'→'v-neck', "
            "터틀넥/크루넥/오프숄더). These sharpen the similarity rerank; omit when unmentioned.\n"
            "  - `length` / `sleeve_length` / `leg_shape`: bottom/length DETAIL, set ONLY when "
            "named. length ('미디'→'midi', 맥시/미니/크롭/발목/무릎 → maxi/mini/crop/ankle/knee), "
            "sleeve_length ('긴팔'→'long', 반팔/민소매 → short/sleeveless), leg_shape ('와이드'→"
            "'wide', 스트레이트/플레어/스키니/테이퍼드). English lowercase.\n"
            "  - `surface` / `texture` / `design_details`: 소재감·질감·디테일 DETAIL, 명시될 때만. "
            "surface ('메탈릭'→'metallic', 새틴광택/시스루/시퀸 → glossy/sheer/sequin), texture "
            "('리브드'→'ribbed', 케이블/레이스/코듀로이/워싱 → cable/lace/corduroy/washed), "
            "design_details ('컷아웃'→'cutout', 슬릿/랩/코르셋/러플 → slit/wrap/corset/ruffle). "
            "단수 토큰 English lowercase — 유사도 rerank 를 날카롭게, 미언급 시 omit.\n"
            "  - 비어패럴 DETAIL (해당 카테고리 상품일 때만): 신발 heel_height ('굽높은'→'high', "
            "플랫/로우/미드)·heel_type ('스틸레토'→'stiletto', 통굽→platform/wedge, 블록)·shaft "
            "('앵클부츠'→'ankle', 롱부츠→knee/thigh)·shoe_toe (오픈토→'open', 포인티드). 가방 "
            "bag_size ('미니백'→'mini', 라지/스몰)·bag_structure (structured/slouchy). 안경 "
            "frame_shape ('캣아이'→'cat_eye', 라운드/aviator). 주얼리 metal_tone ('골드'→'gold', "
            "실버/로즈). English lowercase, 미언급 시 omit.\n"
            "  - `wash` / `graphics`: 데님 워싱·프린트 DETAIL, 명시될 때만. wash ('연청'→'light', "
            "진청→dark, 생지→raw, 워싱→medium, 애시드→acid). graphics ('로고'→'logo', 프린트/그래픽→"
            "graphic, 슬로건/레터링→text, 올오버프린트→allover). English lowercase.\n"
            "  - `mood`: 스타일 무드/트렌드를 사용자가 명시할 때만 (예: '그런지st 바지', "
            "'올드머니룩 니트', '리조트 원피스'). 그 무드 상품만 HARD 필터. 반드시 아래 27개 "
            "폐쇄값 중 정확히 하나(한글 그대로): 미니멀룩·올드머니룩·프렌치시크·시티보이·프레피룩·"
            "아메카지·워크웨어·고프코어·러닝코어·스트릿·그런지·코티지코어·리조트·Y2K·핫걸·"
            "애슬레저/요가·나이트클러빙·다크웨어·해체주의·블록코어·발레코어·포엣코어·그래놀라코어·"
            "란제리코어·슬래커코어·코케트·모리걸. 무드 언급 없으면 omit.\n"
            "\n"
            "[WHEN search_products vs refine_search — DELTA vs PIVOT]\n"
            "Once a search has run this conversation, decide by ONE question: does the new "
            "message introduce a NEW positive target — a different GARMENT type, an OCCASION, "
            "or a POSITIVE brand to switch TO? YES → PIVOT → search_products (a fresh search). "
            "NO → it only ADJUSTS the SAME item (price / color / fit / material / added detail / "
            "EXCLUDE a brand / broaden) → that is a DELTA → refine_search. Negative brand "
            "('자라 빼고') = DELTA (refine); positive brand ('COS 니트', '산드로 걸로') = PIVOT "
            "(search). No prior search yet → always search_products. Genuinely unsure → "
            "search_products.\n"
            "\n"
            "[TEXT_QUERY CANONICAL FORM — REQUIRED for embedding cache stability]\n"
            "Always produce text_query in this exact shape:\n"
            '  "{color} {fit} {garment} {gender}"\n'
            "Rules:\n"
            "  - ENGLISH ONLY, lowercase, space-separated.\n"
            "  - No articles (a / an / the), no prepositions (for / with), no "
            "possessives (men's → men, women's → women).\n"
            "  - Garment must be singular and use the most common term: "
            "'t-shirt' (not 'tee' / 'tees' / 'tshirt'), 'jeans' (not 'denim "
            "pants'), 'sneakers' (not 'trainers'), 'hoodie' (not 'hooded "
            "sweatshirt'), 'sweater' (not 'jumper' / 'knit top').\n"
            "  - Color: prefer 'grey' over 'gray', 'beige' over 'tan/khaki' "
            "unless explicitly khaki.\n"
            "  - Fit: one of 'fitted' / 'slim' / 'regular' / 'loose' / 'oversized' "
            "/ 'wide' / 'straight' / 'cropped'. Omit if unspecified.\n"
            "  - Gender: include 'men' / 'women' ONLY when there's an explicit signal (user text or "
            "picked Vision item — never flip it). When NO signal exists, OMIT gender (the system "
            "appends 'unisex' downstream). Never guess 'women'.\n"
            "  - Omit any field you don't have — never pad with vague words.\n"
            "Examples (gender word ONLY when there's a signal; else omit → system adds unisex):\n"
            '  ✅ "grey fitted t-shirt men"               (user said men)\n'
            '  ✅ "black wide jeans"                       (no signal → omit; system → unisex)\n'
            '  ✅ "beige oversized hoodie"                 (no signal → omit)\n'
            '  ✅ "leather loafers men"                    (picked vision item said men)\n'
            '  ❌ "black wide jeans women"                 (invented gender — no signal existed)\n'
            '  ❌ "Grey Fitted T-Shirt for Men"          (caps, preposition)\n'
            "  ❌ \"men's grey tee that's fitted\"          (possessive, clause)\n"
            '  ❌ "fitted grey t-shirt for men"           (wrong order)\n'
            '  ❌ "a pair of denim jeans"                 (article, redundant)\n'
            "\n"
            "[SITUATION / OCCASION QUERIES → sub_queries]\n"
            "When the user asks for an OUTFIT for a SITUATION rather than a "
            "specific garment (wedding guest, job interview, summer vacation, "
            "date night, festival, first day at work), you CANNOT answer with "
            "one garment. Instead:\n"
            "  - Put the single best-fit garment in `text_query` (canonical form).\n"
            "  - Put 1-2 MORE complementary garments in `sub_queries` (each in "
            "the SAME canonical form). Aim for a coherent outfit: a top/dress + "
            "a layer or shoes/bag — not 3 of the same thing.\n"
            "  - Do NOT include gender words in sub_queries — the system applies "
            "gender to every sub-query automatically.\n"
            "  - Use sub_queries ONLY for situation queries. For a specific "
            "garment request ('grey hoodie') leave sub_queries empty.\n"
            "Example — user: '결혼식 하객룩 추천해줘' (women signal):\n"
            '  text_query="elegant midi dress", '
            'sub_queries=["satin blouse", "slingback heels"]\n'
            "Example — user: '면접 때 입을 옷':\n"
            '  text_query="tailored blazer", '
            'sub_queries=["dress shirt", "straight trousers"]'
        ),
        "args_typeddict": SearchProductsArgs,
        "result_typeddict": SearchProductsResult,
        "dispatch_fn_path": "app.agents.tools.search_products:dispatch",
        "langfuse_span_tag": "tool.search_products",
        "side_effect_doc": "DB RPC + Modal embedding call.",
        "terminates_loop": False,
    },
    "refine_search": {
        "name": "refine_search",
        "description": (
            "Refine the PREVIOUS search by applying a delta — price clamp, style detail boost, "
            "exclude brand, color swap, broaden. Reuses the prior product query under the hood, "
            "so always prefer this over `search_products` when the user is ADJUSTING the same items.\n"
            "REQUIRES a prior search in THIS conversation (there must be results to adjust). Use it "
            "ONLY for a DELTA — an ADJUSTMENT to the SAME item (price / color / fit / material / "
            "added detail / EXCLUDE a brand / broaden). If the user introduces a NEW garment TYPE, a "
            "new OCCASION, or a POSITIVE brand to switch TO ('COS 니트', '산드로 걸로'), that is a "
            "PIVOT — use `search_products` instead (refine would cling to the old item and show the "
            "wrong products). A first budget query with nothing shown yet is also search_products.\n\n"
            "FIELD MAPPING — translate natural language into args. Multiple fields may apply at "
            "once. NEVER drop a user-mentioned detail just because it's hard to phrase:\n\n"
            "  ● PRICE → `max_price` / `min_price` (KRW integer 원, no currency symbol):\n"
            "      '10만원 이하' / 'under 100k won' / 'cheaper'  → max_price=100000\n"
            "      '5만원 이하'  → max_price=50000\n"
            "      '백만원 이상' / 'over 1M'  → min_price=1000000\n"
            "      '20만원 이상 100만원 이하' / '200k-1M'  → min_price=200000, max_price=1000000\n"
            "      'under $100'  → max_price=130000  (USD≈1300원)\n"
            "      Action hint: action='cheaper' for upper-bound clamps.\n\n"
            "  ● STYLE DETAILS → `boost_keywords` (English tokens, lowercased, hyphen-joined when "
            "    multi-word). EVERY descriptor the user mentions MUST become a boost keyword — "
            "    do not skip the ones you don't immediately know how to phrase, just translate:\n"
            "      '크롭이고 사이드 버튼으로 잠그는'  → boost_keywords=['cropped', 'side-button']\n"
            "      '스모킹 탑'                       → boost_keywords=['smocked']\n"
            "      '오프숄더 + 러플'                 → boost_keywords=['off-shoulder', 'ruffled']\n"
            "      '루즈핏 + 와이드'                 → boost_keywords=['relaxed-fit', 'wide-leg']\n"
            "      'darker / more muted'             → boost_keywords=['muted', 'desaturated']\n"
            "      'sleeveless + V-neck'             → boost_keywords=['sleeveless', 'v-neck']\n"
            "      Always include action='refine' alongside style detail boosts.\n\n"
            "  ● COLOR SWAP → `color` (English color word). Use action='color_swap'.\n"
            "      '파란색으로' / 'in blue'  → color='blue', action='color_swap'\n\n"
            "  ● MOOD → `mood` (직전 결과를 특정 스타일 무드로 좁힘, HARD 필터). 한글 27 폐쇄값 "
            "(search_products.mood 와 동일: 그런지·리조트·핫걸·올드머니룩·Y2K·코케트 …).\n"
            "      '리조트st로' / '더 그런지하게'  → mood='리조트' / mood='그런지', action='refine'\n\n"
            "  ● EXCLUDE → `exclude_brands` or `exclude_keywords` + action='exclude'.\n"
            "      '자라 빼고' / 'without Zara'  → exclude_brands=['Zara'], action='exclude'\n\n"
            "  ● BROADEN (0-result recovery) → action='broaden'. Drop subcategory/brand filters; "
            "    keep only core garment + color in boost_keywords.\n\n"
            "COMBINED EXAMPLES (the realistic case — multiple deltas in one user turn):\n"
            "  '검정 크롭 블레이저 사이드버튼 10만원 이하'\n"
            "    → action='refine', boost_keywords=['cropped', 'side-button'], "
            "color='black', max_price=100000\n"
            "  'cheaper, more relaxed fit, under 50k'\n"
            "    → action='cheaper', boost_keywords=['relaxed-fit'], max_price=50000\n\n"
            "FORBIDDEN — do NOT call refine_search with EMPTY boost_keywords / no price / no "
            "color when the user supplied refinement words. Empty args = drift back to the "
            "previous results = user feels ignored. Use this tool ONLY when you actually "
            "translate the user's words into one or more of these fields."
        ),
        "args_typeddict": RefineSearchArgs,
        "result_typeddict": RefineSearchResult,
        "dispatch_fn_path": "app.agents.tools.refine_search:dispatch",
        "langfuse_span_tag": "tool.refine_search",
        "side_effect_doc": "Same as search_products. 1 retry budget per turn.",
        "terminates_loop": False,
    },
    "update_taste": {
        "name": "update_taste",
        "description": (
            "Persist user taste preferences (brand likes/dislikes, keyword affinities). "
            "Use after explicit user feedback like 'I love that brand'."
        ),
        "args_typeddict": UpdateTasteArgs,
        "result_typeddict": UpdateTasteResult,
        "dispatch_fn_path": "app.agents.tools.update_taste:dispatch",
        "langfuse_span_tag": "tool.update_taste",
        "side_effect_doc": "TasteProfile mutation (in-memory or Postgres).",
        "terminates_loop": False,
    },
    "ask_user_clarification": {
        "name": "ask_user_clarification",
        "description": (
            "Send the user an inline-keyboard card asking to clarify intent on one axis. "
            "LAST RESORT ONLY. If the user already named a garment type (hoodie/jeans/"
            "dress/셔츠/청바지…), a brand, or a price/budget, do NOT clarify — call "
            "search_products (or refine_search when they are ADJUSTING the previous "
            "results, e.g. '더 저렴한'). Clarify ONLY when the request is too vague to "
            "form ANY search (e.g. 'recommend something', '미니멀한 옷' with no garment). "
            "The system will REJECT a clarify that has enough signal to search.\n"
            "`axis` MUST be EXACTLY one of these 6 strings (case-sensitive, no variants):\n"
            "  - 'category_pick'              — when user didn't say what garment (top/bottom/outer/dress/shoes/bag)\n"
            "  - 'formality'                  — casual vs business vs formal\n"
            "  - 'fit'                        — slim/regular/oversized/etc\n"
            "  - 'occasion'                   — daily/date/work/party/wedding/etc\n"
            "  - 'subcategory_disambiguation' — narrowing within a category (e.g. shirt: oxford vs linen vs flannel)\n"
            "  - 'generic_fallback'           — when none of the above fit (last resort)\n"
            "DO NOT invent axes like 'gender', 'wearer', 'mood', 'occasion & vibe', etc — they will be rejected.\n"
            "`options` MUST be an ARRAY of 2-6 SHORT, mutually-exclusive choice labels — one label per "
            'array element, each 1-3 words (e.g. ["니트", "가디건", "코트", "셔츠"]). Each becomes its own '
            'tappable button. NEVER put several choices into a single string like ["니트, 가디건, 코트"] — that '
            "renders as one useless button."
        ),
        "args_typeddict": AskUserClarificationArgs,
        "result_typeddict": AskUserClarificationResult,
        "dispatch_fn_path": "app.agents.tools.ask_user_clarification:dispatch",
        "langfuse_span_tag": "tool.ask_user_clarification",
        "side_effect_doc": "Sends a channel message with an inline keyboard.",
        "terminates_loop": False,
    },
    "get_recent_history": {
        "name": "get_recent_history",
        "description": (
            "Fetch this user's recent conversation events (last N entries). "
            "Use when current context is insufficient to decide intent."
        ),
        "args_typeddict": GetRecentHistoryArgs,
        "result_typeddict": GetRecentHistoryResult,
        "dispatch_fn_path": "app.agents.tools.get_recent_history:dispatch",
        "langfuse_span_tag": "tool.get_recent_history",
        "side_effect_doc": "Read-only SELECT on ai.log_conversation_event.",
        "terminates_loop": False,
    },
    "web_search": {
        "name": "web_search",
        "description": (
            "Look something up on the live web. Use ONLY when you cannot answer "
            "from the catalog or your own knowledge — specifically to decode a "
            "STYLE REFERENCE or an UNFAMILIAR brand the user named:\n"
            "  - celebrity / influencer looks and 'OO st(st=스타일)' references "
            "(e.g. '닝닝 공항패션st', '제니st') → search to learn what the look "
            "actually IS (colors, silhouettes, garment types, mood).\n"
            "  - a brand you don't recognize → search to learn its aesthetic.\n"
            "After web_search, TRANSLATE what you learned into a concrete "
            "`search_products` query (color/fit/garment/mood) — the web result "
            "itself is NOT the answer; the catalog products are. Do NOT use "
            "web_search for plain garment requests you can already search "
            "('검정 니트'), for prices, or for stock. Returns short web snippets."
        ),
        "args_typeddict": WebSearchArgs,
        "result_typeddict": WebSearchResult,
        "dispatch_fn_path": "app.agents.tools.web_search:dispatch",
        "langfuse_span_tag": "tool.web_search",
        "side_effect_doc": "External HTTP call to the Tavily search API.",
        "terminates_loop": False,
    },
    "respond": {
        "name": "respond",
        "description": (
            "Send a natural-language reply to the user. Provide ONLY `text` "
            "(your written reply). Do NOT provide cards — product cards are "
            "attached automatically by the system from the most recent search "
            "in this conversation. ALWAYS terminates the agent loop."
        ),
        "args_typeddict": RespondArgs,
        "result_typeddict": RespondResult,
        "dispatch_fn_path": "app.agents.tools.respond:dispatch",
        "langfuse_span_tag": "tool.respond",
        "side_effect_doc": "Sends channel messages (text + optional product cards).",
        "terminates_loop": True,
    },
}

# SPEC-AGENT-V2-CLEANUP-001 — the 8th tool (`suggest_next_step`) is now
# ALWAYS registered (the AGENT_V3_PROACTIVE_ENABLED flag was removed). The
# ReAct agent is the permanent, only topology.
# @MX:NOTE: [AUTO] 8-tool registry — suggest_next_step is unconditional
REGISTRY["suggest_next_step"] = {
    "name": "suggest_next_step",
    "description": (
        "Proactively send the user a follow-up options card (similar items, "
        "fit change, different mood, broaden). Use when results are weak "
        "(candidates_count < 3) or to offer next steps. Does NOT terminate "
        "the loop — follow with `respond` once the user has options."
    ),
    "args_typeddict": SuggestNextStepArgs,
    "result_typeddict": SuggestNextStepResult,
    "dispatch_fn_path": "app.agents.tools.suggest_next_step:dispatch",
    "langfuse_span_tag": "tool.suggest_next_step",
    "side_effect_doc": "Sends a channel message with an inline keyboard (reuses adapter).",
    "terminates_loop": False,
}

TOOL_NAMES: tuple[str, ...] = tuple(REGISTRY.keys())


# ── Args validation (REQ-AGENT-TOOL-DISPATCH-001) ─────────────────────────


def validate_args(tool_name: str, args: dict[str, Any]) -> tuple[bool, str | None]:
    """Lightweight TypedDict validation.

    Returns (ok, error_message). On invalid:
    - tool not in REGISTRY → (False, "unknown_tool")
    - args is not a dict → (False, "args_not_dict")
    - unknown keys present → (False, "unknown_keys: ...")
    - missing required keys (TypedDict `__required_keys__`) → (False, "missing_required: ...")

    Does not deeply check value types — TypedDict is structural and the LLM via
    OpenAI Tools API should already produce schema-compliant JSON. Defense in depth.
    """
    if tool_name not in REGISTRY:
        return False, f"unknown_tool: {tool_name}"
    if not isinstance(args, dict):
        return False, "args_not_dict"

    td = REGISTRY[tool_name]["args_typeddict"]
    allowed = set(getattr(td, "__annotations__", {}).keys())
    provided = set(args.keys())
    unknown = provided - allowed
    if unknown:
        return False, f"unknown_keys: {sorted(unknown)}"

    required = set(getattr(td, "__required_keys__", frozenset()))
    missing = required - provided
    if missing:
        return False, f"missing_required: {sorted(missing)}"

    # P1-5: lightweight isinstance enforcement for type-critical fields only.
    # An LLM passing `top_k: {"nested": 1}` or `n: "abc"` would otherwise pass
    # validation and crash deep in a caller. Not a full schema validator —
    # only the fields whose wrong type breaks a downstream call.
    # P1-C: `top_k`/`n` are int-only; `min_price`/`max_price` are typed
    # `float | None` (SearchProductsArgs/RefineSearchArgs) — the LLM
    # legitimately sends e.g. 59.99, so accept int OR float there.
    # bool is a subclass of int — reject it explicitly in both groups.
    #
    # AUTO-CAST (2026-05-20): Haiku 4.5 occasionally sends type-mismatched JSON
    # (`top_k="15"`, `boost_keywords="t-shirt"`, `min_price="50000"`). Strict
    # rejection burns a ReAct iter per occurrence — turns saw 3 consecutive
    # bad_args eating half the iter budget before search even started. Safe
    # coercions are done in-place; only genuinely unconvertible values reject.
    #
    # CRITICAL: never `list(v)` a string — `list("t-shirt")` explodes to
    # `["t","-","s","h","i","r","t"]` which contaminates the embedded query
    # (the bug the previous strict check was avoiding). Use `[v]` to wrap.
    for key in ("top_k", "n"):
        if key in args:
            v = args[key]
            if isinstance(v, bool):
                return False, f"bad_type: {key} must be int"
            if isinstance(v, int):
                continue
            if isinstance(v, float) and v.is_integer():
                args[key] = int(v)
                continue
            if isinstance(v, str):
                stripped = v.strip()
                if stripped.lstrip("-").isdigit():
                    args[key] = int(stripped)
                    continue
            return False, f"bad_type: {key} must be int"
    for key in ("min_price", "max_price"):
        if key in args:
            v = args[key]
            if isinstance(v, bool):
                return False, f"bad_type: {key} must be number"
            if isinstance(v, (int, float)):
                continue
            if isinstance(v, str):
                try:
                    args[key] = float(v.strip())
                    continue
                except (ValueError, AttributeError):
                    pass
            return False, f"bad_type: {key} must be number"
    for key in (
        "brand_likes",
        "brand_dislikes",
        "keyword_likes",
        "keyword_dislikes",
        "event_types",
        "options",
        # 2026-05-20: refine_search list fields. Without strict check, an LLM
        # passing a string ("t-shirt") flows through `list(...)` in dispatch
        # and explodes into per-character tokens (["t","-","s","h","i","r","t"]).
        # Auto-cast (2026-05-20): wrap single str in `[v]` so a lone keyword
        # passes safely; reject anything else (dict, int, etc.).
        "boost_keywords",
        "exclude_keywords",
        "exclude_brands",
    ):
        if key in args:
            v = args[key]
            if isinstance(v, list):
                continue
            if isinstance(v, str):
                # Lone non-empty string → 1-element list. Empty string drops the
                # field entirely (LLM occasionally sends "" as a no-op).
                stripped = v.strip()
                args[key] = [stripped] if stripped else []
                continue
            return False, f"bad_type: {key} must be list"

    # P0-1 (260521 V3 eval): Literal-value enforcement for type-critical enum
    # fields. The structural TypedDict check above accepts ANY string for
    # `axis`, then the dispatcher returns `invalid_axis:gender` and the LLM
    # often retries with the SAME invalid value (an info-poor reject loop).
    # Reject here so the validator error message itself spells out the valid
    # set — `bad_axis: 'gender' not in ['category_pick', ...]` — which the
    # ReAct loop returns to the LLM as result_summary, enabling self-correction
    # on the very next iter. Scoped to `ask_user_clarification` until other
    # tools demand the same.
    if tool_name == "ask_user_clarification" and "axis" in args:
        valid_axes = ("category_pick", "formality", "fit", "occasion", "subcategory_disambiguation", "generic_fallback")
        axis = args["axis"]
        if not isinstance(axis, str) or axis not in valid_axes:
            return False, f"bad_axis: {axis!r} not in {list(valid_axes)}"

    return True, None
