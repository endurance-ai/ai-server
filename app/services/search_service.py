"""Search service (SPEC-ARCH-AI-001 PR1 + PR2; v6-migrated by SPEC-SEARCH-V6-001).

Search orchestration. PR2 (REQ-AI-002): the RPC name and param-dict
construction live in app/infrastructure/repositories/search_repository.py —
this service delegates both to SearchRepository.

v6 embedding-first → query_text/enhance_query RPC path retired (module
retained, dormant): enhance_query_step (SPEC-PIPELINE-001, flag-off by
default) still runs in the pipeline but its output is no longer consumed for
RPC params — search_products_v6 has no text param.

The actual RPC invocation goes through the
app.pipeline.search.DatabaseProvider.rpc monkeypatch seam (dispatched inside
SearchRepository).
"""

import logging
import re
from contextvars import ContextVar
from typing import Any

from app.core.config import settings
from app.infrastructure.repositories.category_family import to_canonical_family
from app.infrastructure.repositories.search_repository import SearchRepository
from app.infrastructure.repositories.search_repository import (
    embedding_to_pgvector as _embedding_to_pgvector,
)

# search_rpc_contract imports only pydantic/typing (no app.* back-edge) so a
# module-top import is cycle-free -- no lazy/seam pattern needed here.
from app.infrastructure.repositories.search_rpc_contract import RpcContractError
from app.infrastructure.repositories.subcategory_vocab import normalize_subcategory
from app.pipeline.state import PipelineState
from app.scoring.brand_2tower_rescore import rescore as _brand_2tower_rescore
from app.scoring.personalize_rerank import RerankWeights
from app.scoring.personalize_rerank import rerank as _personalize_rerank

logger = logging.getLogger(__name__)

# 색(및 subcategory) 정밀 필터가 재고 부족으로 relax·drop 됐을 때의 신호를
# 상위 에이전트 dispatch 로 전파하는 채널. 검색 파이프라인은 반환값이 후보
# 리스트뿐이라 "요청 색이 사실상 없어서 유사상품으로 채웠다"는 사실이 사라졌고,
# 에이전트는 그걸 모른 채 "핑크로 바꿨어!"라고 거짓 확답을 했다(2026-08-24 실
# 트레이스). ContextVar 라 요청(async task)별로 격리된다. dispatch 가 검색 직후
# 읽어 result.notice 로 실어 모델이 정직하게 안내하게 한다. None=relax 없음.
color_relax_ctx: ContextVar[dict[str, Any] | None] = ContextVar("color_relax_ctx", default=None)

# Back-compat re-export: app/pipeline/search.py shim re-exports this name and
# tests reference _embedding_to_pgvector. The implementation now lives in the
# repository (single source for the pgvector format alongside the param map).
embedding_to_pgvector = _embedding_to_pgvector


def _resolve_precision_filters(item: Any) -> tuple[str | None, str | None, str | None]:
    """`(subcategory, subcategory_family, color_family)` 정밀 필터 해석.

    v6 의 `p_subcategory`/`p_color_family` 는 EXACT 매치이고 어느 rung 에서도
    완화되지 않는다 (실물 함수 정의 확인) — 모르는 값을 보내면 0결과가 되므로
    subcategory 는 canonical vocabulary 에 매칭된 값만 통과시킨다 (fail-open
    → None).

    subcategory 소스 우선순위: `item.subcategory` (Vision v2 / mobile) →
    `item.category` (LLM 자유형 arg 가 "hoodie" 같은 subcategory 레벨 단어인
    경우). 매칭되면 family 도 함께 반환 — subcategory 가 family 보다 강한
    신호라(백엔드 배치: hoodie→tops, sweater→knitwear — Vision 분류와 다름)
    호출부가 family gate 를 이 family 로 정렬한다.
    """
    sub: str | None = None
    sub_family: str | None = None
    if settings.SEARCH_SUBCATEGORY_FILTER_ENABLED:
        sub, sub_family = normalize_subcategory(getattr(item, "subcategory", None))
        if sub is None:
            sub, sub_family = normalize_subcategory(getattr(item, "category", None))

    # 2026-07-29 — 색 출처가 products.color(크롤러 원본값) → product_features
    # (VLM primary_color) 로 바뀌면서 alias 보정이 불필요해졌다. 구 _COLOR_ALIAS
    # (multi→multicolor, gray→grey) 는 크롤러가 뱉던 자유형 문자열에 Vision enum
    # 을 억지로 맞추려던 땜빵이었는데, primary_color 는 Vision 과 **정확히 같은
    # 16 family enum** 이라 그대로 넘기면 맞는다. 대문자로 보내 RPC 의
    # `= UPPER(p_color_family)` 와 표기를 일치시킨다.
    color: str | None = None
    if settings.SEARCH_COLOR_FILTER_ENABLED:
        c = str(getattr(item, "color_family", None) or "").strip().upper()
        if c:
            color = c
    return sub, sub_family, color


def _resolve_gender(req: Any) -> str | None:
    """`RecommendRequest.gender` → v6 `p_gender` ('men'|'women'|None).

    2026-07-16 — 상품 레벨 gender 하드 필터 (골든셋 2차 실측: 남성 쿼리에
    여성 상품 누수). 'men'/'women' 만 필터로 태우고, 'unisex'/미확인/그 외
    값은 None(필터 off). unisex 상품은 어느 성별 요청에도 항상 포함된다.
    시맨틱 제약이므로 완화 재시도에서 제거하지 않는다.

    2026-08-04 — 이 함수는 그동안 바뀌지 않았다. 바뀐 건 RPC 쪽 매칭 소스뿐이다:
    VLM 우선 3단 다리 → 크롤러 정본 우선 → migration 104 VALIDATE 이후
    `p.gender && ARRAY[p_gender,'unisex']` 단일 출처로 축약.
    호출부 계약('men'|'women'|None)은 전 구간 동일하다."""
    if not settings.SEARCH_GENDER_FILTER_ENABLED:
        return None
    g = str(getattr(req, "gender", None) or "").strip().lower()
    return g if g in ("men", "women") else None


def _query_pinned_axes(item: Any) -> frozenset[str]:
    """Feature axes the query specified explicitly → excluded from the feature
    re-rank (adaptive α, "blanks = taste"): learned taste fills only the axes the
    user left open and never overrides an explicit intent (the color/subcategory
    hard filters already enforce the pinned ones).

    Only color/fit/material are expressible in the request contract
    (`AnalyzedItem.color_family`/`fit`/`fabric`); pattern/neckline are never
    query-pinned, so taste always speaks for them.
    """
    pinned: set[str] = set()
    if str(getattr(item, "color_family", None) or "").strip():
        pinned.add("color")
    if str(getattr(item, "fit", None) or "").strip():
        pinned.add("fit")
    if str(getattr(item, "fabric", None) or "").strip():
        pinned.add("material")
    return frozenset(pinned)


# 입력 fit 표현 → product_features.feature_metadata.fit vocab
# (regular/relaxed/slim/longline/oversized/cropped/skinny/boxy) 정규화 맵.
_FIT_NORM: dict[str, set[str]] = {
    "oversized": {"oversized"},
    "oversize": {"oversized"},
    "loose": {"relaxed", "oversized"},
    "relaxed": {"relaxed"},
    "boxy": {"oversized", "relaxed", "boxy"},
    "drop-shoulder": {"oversized"},
    "wide": {"relaxed"},
    "wide-leg": {"relaxed"},
    "wideleg": {"relaxed"},
    "regular": {"regular"},
    "slim": {"slim", "skinny"},
    "skinny": {"skinny"},
    "fitted": {"slim", "skinny"},
    "cropped": {"cropped"},
    "crop": {"cropped"},
    "longline": {"longline"},
}

# 한글 fit 표현 (검색어가 한글로 남는 경우 — searchQueryKo/미보강 쿼리).
_FIT_KO: dict[str, set[str]] = {
    "오버핏": {"oversized"},
    "오버사이즈": {"oversized"},
    "루즈": {"relaxed", "oversized"},
    "릴렉스": {"relaxed"},
    "와이드": {"relaxed"},
    "박시": {"oversized", "relaxed", "boxy"},
    "슬림": {"slim", "skinny"},
    "스키니": {"skinny"},
    "크롭": {"cropped"},
    "레귤러": {"regular"},
}

# 영/한 material 표현 → feature_metadata.material[] canonical 토큰
# (2026-08 dev 실측 분포 기준). 오탐 위험 단일음절 한글(면/청/울/마)은 제외.
_MATERIAL_NORM: dict[str, str] = {
    # english (canonical self-map + 흔한 변형)
    "cotton": "cotton",
    "leather": "leather",
    "polyester": "polyester",
    "poly": "polyester",
    "metal": "metal",
    "silk": "silk",
    "knit": "knit",
    "knitted": "knit",
    "wool": "wool",
    "nylon": "nylon",
    "denim": "denim",
    "suede": "suede",
    "linen": "linen",
    "mesh": "mesh",
    "canvas": "canvas",
    "satin": "satin",
    "cashmere": "cashmere",
    "fleece": "fleece",
    "tweed": "tweed",
    "jersey": "jersey",
    "velvet": "velvet",
    "corduroy": "corduroy",
    "ripstop": "ripstop",
    "chiffon": "chiffon",
    "goretex": "gore-tex",
    "gore-tex": "gore-tex",
    # korean (다음절·비모호 토큰만)
    "코튼": "cotton",
    "가죽": "leather",
    "레더": "leather",
    "데님": "denim",
    "니트": "knit",
    "실크": "silk",
    "린넨": "linen",
    "리넨": "linen",
    "스웨이드": "suede",
    "캐시미어": "cashmere",
    "코듀로이": "corduroy",
    "골덴": "corduroy",
    "트위드": "tweed",
    "벨벳": "velvet",
    "새틴": "satin",
    "사틴": "satin",
    "플리스": "fleece",
    "후리스": "fleece",
    "나일론": "nylon",
    "폴리에스터": "polyester",
    "시폰": "chiffon",
    "고어텍스": "gore-tex",
    "저지": "jersey",
    "메쉬": "mesh",
    "캔버스": "canvas",
}


# 영/한 pattern 표현 → feature_metadata.pattern canonical 토큰
# (2026-08 dev 실측: solid/graphic/striped/checked/floral/logo/dot/animal/
# colorblock/camo/heathered/abstract). solid 는 카탈로그 ~75% 라 target 에서
# 제외(부스트 무의미) — 변별 패턴만 매핑한다.
_PATTERN_NORM: dict[str, str] = {
    # english
    "striped": "striped",
    "stripe": "striped",
    "pinstripe": "striped",
    "checked": "checked",
    "check": "checked",
    "plaid": "checked",
    "gingham": "checked",
    "tartan": "checked",
    "floral": "floral",
    "flower": "floral",
    "dot": "dot",
    "dotted": "dot",
    "polka": "dot",
    "camo": "camo",
    "camouflage": "camo",
    "animal": "animal",
    "leopard": "animal",
    "cheetah": "animal",
    "zebra": "animal",
    "snake": "animal",
    "graphic": "graphic",
    "logo": "logo",
    "colorblock": "colorblock",
    "color-block": "colorblock",
    "colorblocked": "colorblock",
    "abstract": "abstract",
    # korean (다음절·비모호 토큰만)
    "스트라이프": "striped",
    "줄무늬": "striped",
    "체크": "checked",
    "격자": "checked",
    "플로럴": "floral",
    "꽃무늬": "floral",
    "도트": "dot",
    "땡땡이": "dot",
    "물방울": "dot",
    "카모": "camo",
    "카무플라주": "camo",
    "레오파드": "animal",
    "호피": "animal",
    "지브라": "animal",
    "그래픽": "graphic",
    "컬러블록": "colorblock",
}


def _ascii_word_re(key: str) -> re.Pattern[str]:
    r"""`\b`-경계 매칭 패턴 (하이픈은 공백/하이픈 허용). 영문 vocab 전용."""
    body = re.escape(key).replace(r"\-", r"[\s-]?")
    return re.compile(rf"\b{body}\b")


_FIT_EN_PATTERNS: list[tuple[re.Pattern[str], set[str]]] = [(_ascii_word_re(k), v) for k, v in _FIT_NORM.items()]
_MATERIAL_EN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_ascii_word_re(k), v) for k, v in _MATERIAL_NORM.items() if k.isascii()
]
_PATTERN_EN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (_ascii_word_re(k), v) for k, v in _PATTERN_NORM.items() if k.isascii()
]
_FIT_KO_ITEMS = list(_FIT_KO.items())
_MATERIAL_KO_ITEMS = [(k, v) for k, v in _MATERIAL_NORM.items() if not k.isascii()]
_PATTERN_KO_ITEMS = [(k, v) for k, v in _PATTERN_NORM.items() if not k.isascii()]


def _extract_fit_from_text(text: str) -> set[str]:
    """쿼리 텍스트에서 fit 토큰 추출 → canonical fit vocab 집합.

    에이전트가 fit 을 구조화 인자로 안 넘기고 free-text(text_query)에만 담는
    실측 패턴("oversized hoodie" → fit 인자 없음) 을 보완한다. 영문은 `\b`
    경계, 한글은 부분일치.
    """
    if not text:
        return set()
    low = text.lower()
    out: set[str] = set()
    for pat, vals in _FIT_EN_PATTERNS:
        if pat.search(low):
            out |= vals
    for ko, vals in _FIT_KO_ITEMS:
        if ko in text:
            out |= vals
    return out


def _extract_material_from_text(text: str) -> set[str]:
    """쿼리 텍스트에서 material 토큰 추출 → feature_metadata.material canonical 집합."""
    if not text:
        return set()
    low = text.lower()
    out: set[str] = set()
    for pat, canon in _MATERIAL_EN_PATTERNS:
        if pat.search(low):
            out.add(canon)
    for ko, canon in _MATERIAL_KO_ITEMS:
        if ko in text:
            out.add(canon)
    return out


def _extract_pattern_from_text(text: str) -> set[str]:
    """쿼리 텍스트에서 변별 pattern 토큰 추출 → feature_metadata.pattern canonical 집합.

    solid 는 매핑에 없어 자동 제외(카탈로그 기본값이라 부스트 무의미).
    """
    if not text:
        return set()
    low = text.lower()
    out: set[str] = set()
    for pat, canon in _PATTERN_EN_PATTERNS:
        if pat.search(low):
            out.add(canon)
    for ko, canon in _PATTERN_KO_ITEMS:
        if ko in text:
            out.add(canon)
    return out


def _query_target_attrs(item: Any) -> dict[str, set[str]]:
    """쿼리가 명시한 target 속성 → 후보 정렬 boost 축("우와 비슷하다").

    fit/material/pattern 축을 취한다(color/subcategory 는 RPC 하드게이트라 모든
    후보가 이미 일치 → 정렬 제외). 구조화 인자(item.fit/fabric)를 우선하되,
    에이전트가 보통 이를 안 채우고 free-text 쿼리에만 담으므로 `search_query`(+KO)
    텍스트에서 결정론적으로 보완 추출한다(pattern 은 request 계약에 필드가 없어
    텍스트 전용). 값은 feature vocab 집합으로 정규화.
    """
    out: dict[str, set[str]] = {}
    qtext = " ".join(s for s in (getattr(item, "search_query", None), getattr(item, "search_query_ko", None)) if s)

    # 무드(v2.6 final_tags, 27 폐쇄값) — 명시 mood arg 로만. 예전엔 하드필터였으나
    # rerank 부스트축으로 전환(하드필터 제거). final_tags 는 한글이라 소문자화는
    # 사실상 무해(candidate 측도 동일 정규화).
    mood = str(getattr(item, "mood", None) or "").strip().lower()
    if mood:
        out["mood"] = {mood}

    # fit — 구조화 인자 우선, 없으면 쿼리 텍스트에서 추출.
    fit = str(getattr(item, "fit", None) or "").strip().lower()
    fit_vals = (_FIT_NORM.get(fit) or ({fit} if fit else set())) or _extract_fit_from_text(qtext)
    if fit_vals:
        out["fit"] = set(fit_vals)

    # material — 구조화 fabric + 쿼리 텍스트 추출(합집합).
    mat_vals: set[str] = set()
    fab = str(getattr(item, "fabric", None) or "").strip().lower()
    if fab:
        mat_vals.add(_MATERIAL_NORM.get(fab, fab))
    mat_vals |= _extract_material_from_text(qtext)
    if mat_vals:
        out["material"] = mat_vals

    # pattern — 구조화 인자(item.pattern) 우선 + 쿼리 텍스트 추출(합집합).
    pat_vals: set[str] = set()
    pat = str(getattr(item, "pattern", None) or "").strip().lower()
    if pat:
        pat_vals.add(_PATTERN_NORM.get(pat, pat))
    pat_vals |= _extract_pattern_from_text(qtext)
    if pat_vals:
        out["pattern"] = pat_vals

    # neckline — 구조화 인자(item.neckline)만(텍스트 추출기 없음). feature_metadata.neckline 정렬.
    neck = str(getattr(item, "neckline", None) or "").strip().lower()
    if neck:
        out["neckline"] = {neck}

    # v2.6 축(length/sleeve_length/leg_shape) — 명시 arg 로만. _attach_feature_metadata 가
    # product_features_v26.attr 값을 feature_metadata 에 머지해 둔다. length 'cropped'→
    # 'crop' 만 정규화(중복 vocab).
    length = str(getattr(item, "length", None) or "").strip().lower()
    if length:
        out["length"] = {"crop" if length == "cropped" else length}
    sleeve = str(getattr(item, "sleeve_length", None) or "").strip().lower()
    if sleeve:
        out["sleeve_length"] = {sleeve}
    leg = str(getattr(item, "leg_shape", None) or "").strip().lower()
    if leg:
        out["leg_shape"] = {leg}

    # v2.6 스타일 디테일축 — surface(스칼라)/texture/design_details. _attach_feature_metadata
    # 가 v26.attr 에서 머지(texture/design_details 는 배열 그대로).
    surface = str(getattr(item, "surface", None) or "").strip().lower()
    if surface:
        out["surface"] = {surface}
    texture = str(getattr(item, "texture", None) or "").strip().lower()
    if texture:
        out["texture"] = {texture}
    design = str(getattr(item, "design_details", None) or "").strip().lower()
    if design:
        out["design_details"] = {design}

    # v2.6 비어패럴 조건부축(신발/가방/안경/주얼리) — 공유 리스트로 일괄 세팅.
    from app.scoring.personalize_rerank import NONAPPAREL_SCALAR_AXES

    for _ax in NONAPPAREL_SCALAR_AXES:
        _v = str(getattr(item, _ax, None) or "").strip().lower()
        if _v:
            out[_ax] = {_v}

    # v2.6 wash(데님 워싱)/graphics(로고·프린트).
    wash = str(getattr(item, "wash", None) or "").strip().lower()
    if wash:
        out["wash"] = {wash}
    graphics = str(getattr(item, "graphics", None) or "").strip().lower()
    if graphics:
        out["graphics"] = {graphics}

    return {k: v for k, v in out.items() if v}


# v26 → v1.1 neckline vocab 정규화(fill 시에만). 매핑 없는 값은 채우지 않는다
# (candidate 쪽 neckline 은 v1.1 vocab 이므로 target 과 매칭되려면 통일 필요).
_V26_NECKLINE_TO_V11: dict[str, str] = {
    "v": "v-neck",
    "round": "crew",
    "square": "square",
    "halter": "halter",
    "off_shoulder": "off-shoulder",
    "turtleneck": "turtleneck",
    "collar": "collared",
}
# v1.1 feature_metadata 가 이미 갖는 축 — v1.1 값 우선(거동 불변), 없을 때만 v26 로 채운다.
# (누수 픽스: v1.1 row 없는 v26-only ~26k 상품이 material/pattern/color/neckline rerank 를
# 못 받던 문제. 나머지 신규축은 v1.1 에 없어 override=fill 이라 그대로.)
_V26_FILL_KEYS = frozenset({"material", "pattern", "primary_color", "neckline"})


def _merge_v26_attrs(fm: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any] | None:
    """v1.1 feature_metadata + v26 축 머지(순수함수, 테스트 용이).

    - 신규축(length/surface/신발·가방 등, v1.1 에 없음): override=fill.
    - _V26_FILL_KEYS(material/pattern/color/neckline, v1.1 이 이미 가짐): v1.1 우선, 빈 곳만 채움.
    - neckline: v26 coarse → v1.1 vocab 정규화, 매핑 없으면 스킵.
    - v26 없으면 fm 그대로.
    """
    if not (extra and any(extra.values())):
        return fm
    merged = dict(fm) if isinstance(fm, dict) else {}
    for k, val in extra.items():
        if not val:
            continue
        if k in _V26_FILL_KEYS:
            if merged.get(k):
                continue
            if k == "neckline":
                val = _V26_NECKLINE_TO_V11.get(str(val).strip().lower())
                if not val:
                    continue
            merged[k] = val
        else:
            merged[k] = val
    return merged


async def _attach_feature_metadata(rows: list[dict[str, Any]]) -> None:
    """Attach `public.product_features.feature_metadata` to candidate rows in place.

    v6 RPC rows carry only distance/brand/… — the visual features live in a
    separate table, so the feature re-rank fetches them for the current candidate
    set in one batch. Fail-open: pool down or a missing row just leaves
    `feature_metadata` unset (scored as neutral).
    """
    ids = [int(r["id"]) for r in rows if str(r.get("id") or "").isdigit()]
    if not ids:
        return
    from app.providers import db_pool

    pool = db_pool._pool  # noqa: SLF001
    if pool is None:
        return
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT product_id, feature_metadata FROM public.product_features WHERE product_id = ANY(%s)",
            (ids,),
        )
        meta = {int(r[0]): r[1] for r in await cur.fetchall()}
        # v2.6 enrichment (product_features_v26.attr) 머지. 신규축(length/surface/신발·가방 등)은
        # v1.1 에 없어 override, 겹치는 축(material/pattern/color/neckline)은 v1.1 우선·빈 곳만 채움
        # (_V26_FILL_KEYS). fail-open: v26 없으면 v1.1 만.
        await cur.execute(
            "SELECT product_id, attr->>'length', attr->>'sleeve_length', attr->>'leg_shape', "
            "attr->>'surface', attr->'texture', attr->'design_details', "
            "attr->>'heel_type', attr->>'heel_height', attr->>'shaft', attr->>'shoe_toe', "
            "attr->>'bag_size', attr->>'bag_structure', attr->>'frame_shape', attr->>'metal_tone', "
            "attr->'material', attr->>'pattern', attr->>'primary_color', attr->>'neckline', "
            "attr->>'wash', attr->>'graphics', final_tags "
            "FROM public.product_features_v26 WHERE product_id = ANY(%s)",
            (ids,),
        )
        v26 = {
            int(r[0]): {
                "length": r[1],
                "sleeve_length": r[2],
                "leg_shape": r[3],
                "surface": r[4],
                "texture": r[5],
                "design_details": r[6],
                "heel_type": r[7],
                "heel_height": r[8],
                "shaft": r[9],
                "shoe_toe": r[10],
                "bag_size": r[11],
                "bag_structure": r[12],
                "frame_shape": r[13],
                "metal_tone": r[14],
                # fill 축(v1.1 이 이미 갖는 것 — 없을 때만 채움).
                "material": r[15],
                "pattern": r[16],
                "primary_color": r[17],
                "neckline": r[18],
                # 신규 override 축(v1.1 미보유).
                "wash": r[19],
                "graphics": r[20],
                # v2.6 스타일 무드 태그(final_tags 배열, 27 폐쇄값) → 무드 rerank 축.
                "mood_tags": r[21],
            }
            for r in await cur.fetchall()
        }
    for r in rows:
        rid = str(r.get("id") or "")
        if not rid.isdigit():
            continue
        pid = int(rid)
        r["feature_metadata"] = _merge_v26_attrs(meta.get(pid), v26.get(pid))


# NOTE — `_apply_mood_filter`(v2.6 무드 하드필터)는 제거됨(2026-09-04). 무드는
# 이제 하드필터가 아니라 `_query_target_attrs` 의 mood 축 + personalize_rerank 의
# attr_mood 부스트로 처리한다(리콜 보존 + 무드 매칭 상단). 하드필터의 0매치
# fail-open 랜덤·근접무드 배제·필터 후 무드-blind 정렬 문제를 해소.


async def search_service(state: PipelineState) -> PipelineState:
    if state.embedding is None:
        raise RuntimeError("search_step requires state.embedding (call embed_step first)")

    req = state.request
    state.start("search")
    # relax 신호 초기화 (이 요청에서 relax 없으면 None 유지).
    color_relax_ctx.set(None)

    # v6 embedding-first → query_text/enhance_query RPC path retired (module
    # retained, dormant). search_products_v6 has no text param: query_embedding
    # is the sole ranking signal, so enhance_query_step's output is no longer
    # consumed here (the step still runs in the pipeline, just not fed to RPC).

    # REQ-AI-002: param mapping + RPC name owned solely by SearchRepository.
    # SPEC-SEARCH-V6-001 family gate: pass the real Vision/app item category;
    # SearchRepository normalizes it to one of the 20 canonical tokens.
    style_node_code = req.style_node.primary if req.style_node else None

    # 2026-07-15 정밀 필터 (백엔드 subcategory 정규화 연동 + SPEC-SEARCH-V6-COLOR
    # 보강): subcategory 는 canonical vocab 매칭 성공 값만, color 는 alias 보정
    # 후 전달. subcategory 가 resolve 되면 family gate 는 **항상** 그 subcategory
    # 의 소속 family 로 정렬한다 — Vision 분류(hoodie=Outer, sweater=Top)와
    # 백엔드 배치(hoodie→tops, sweater→knitwear)가 다르기 때문에, item 의
    # category family 를 그대로 쓰면 "outerwear family + hoodie EXACT" 처럼
    # 교집합 0 인 조합이 나온다. subcategory 미해석 시엔 기존 category 경로.
    sub_norm, sub_family, color_norm = _resolve_precision_filters(req.item)
    category_for_gate = req.item.category
    if sub_family is not None:
        category_for_gate = sub_family

    gender_norm = _resolve_gender(req)

    # SPEC-SEARCH-HYBRID-001 — 순수 텍스트 쿼리(state.hybrid_text)이고 플래그 ON 이면
    # 이미지⊕텍스트 임베딩 블렌드 RPC 로 라우팅. 그 외(사진/blended/pinned-anchor,
    # 또는 플래그 OFF)는 v6 이미지 단독 경로로 byte-identical. 게이트 키가 동일해
    # 아래 완화 재시도·진단 로깅은 두 경로를 균일하게 다룬다.
    use_hybrid = settings.SEARCH_HYBRID_ENABLED and getattr(state, "hybrid_text", False)
    if use_hybrid:
        params = SearchRepository.build_params_hybrid(
            embedding=state.embedding,
            brand_filter=req.brand_filter,
            category=category_for_gate,
            style_node_code=style_node_code,
            color_family=color_norm,
            subcategory=sub_norm,
            gender=gender_norm,
            w_text=settings.SEARCH_HYBRID_W_TEXT,
            pool=settings.SEARCH_HYBRID_POOL,
            name_query=(str(getattr(req.item, "name_query", None) or "").strip() or None),
        )
        _do_search = SearchRepository.search_hybrid
        rpc_name = "search_products_hybrid_v1"
    else:
        params = SearchRepository.build_params(
            embedding=state.embedding,
            brand_filter=req.brand_filter,
            category=category_for_gate,
            style_node_code=style_node_code,
            color_family=color_norm,
            subcategory=sub_norm,
            gender=gender_norm,
        )
        _do_search = SearchRepository.search
        rpc_name = "search_products_v6"

    # Family-gate verification hook (SPEC-SEARCH-V6-001). Distinct from v6's
    # OWN post-RPC node-presence `degraded` (logged separately below): this
    # line surfaces the category-miss → `other` gate-skip BEFORE the RPC so
    # "category-miss other-skip" is never conflated with node-presence
    # `degraded`. raw=Vision/app value, canonical=resolved 20-token.
    canonical = to_canonical_family(category_for_gate)
    logger.info(
        "[STEP 4.5][search] category raw=%r → canonical=%r family_gate=%s "
        "subcat=%r color=%r gender=%r style_node=%s→id=%s",
        req.item.category,
        canonical,
        "active" if canonical != "other" else "skipped(other)",
        sub_norm,
        color_norm,
        gender_norm,
        style_node_code,
        params.get("p_style_node_id"),
    )

    # RPC 입력 파라미터 — query_embedding 은 길어서 dim 만
    diag_params = {k: v for k, v in params.items() if k != "query_embedding"}
    diag_params["query_embedding_dim"] = len(state.embedding)
    logger.info("[STEP 4.5][search] DB RPC 호출 시작 — fn=%s params=%s", rpc_name, diag_params)

    # REQ-AI-006: SearchRepository.search raises RpcContractError when the RPC
    # returns a row violating the documented contract. Catch it at the SERVICE
    # boundary (the contract-raise stays at the repository so the
    # validate_rpc_rows unit assertions remain valid). We log ONE structured
    # ERROR line with ONLY the row index + exception class name -- explicitly
    # NOT str(exc)/the pydantic detail/row values (no row content in logs or
    # response: avoids info disclosure). Then fail OPEN: rows=[] and continue
    # the normal empty-result path (matches the codebase's existing
    # raw_count=0 resilience pattern; persistent drift no longer 502s/DoSes).
    # The drift is still surfaced (REQ-AI-006 "surface, not silent") via this
    # ERROR log + the Langfuse trace, without leaking row content.
    try:
        rows = await _do_search(params)
    except RpcContractError as exc:
        logger.error(
            "[STEP 4.5][search] RPC contract drift -- failing open to empty result (row_index=%s exc=%s)",
            exc.row_index,
            type(exc).__name__,
        )
        rows = []

    # 2026-07-15 정밀 필터 완화 재시도: v6 는 p_subcategory/p_color_family 를
    # 어느 rung 에서도 완화하지 않으므로 (실물 함수 정의 확인) AI 서버가
    # 완화 rung 을 맡는다 — subcategory 40% NULL(라벨 미부여 상품은 EXACT
    # 매치에서 무조건 탈락) + color 크롤러 원본값 특성상 정밀 필터가 풀을
    # 과도하게 조일 수 있다. 결과가 부족하면 두 필터를 빼고 1회 재시도,
    # 정밀 매치(1차)를 앞에 두고 id dedup 병합 — 정확도는 지키고 리콜만 보강.
    relaxed_color_family: str | None = None
    if len(rows) < settings.SEARCH_FILTER_RELAX_MIN and (
        params.get("p_subcategory") is not None or params.get("p_color_family") is not None
    ):
        # 색 게이트가 relax 되면 그 색을 rerank 소프트 부스트로 되살려 exact-color 를
        # 상단에 유지한다(재고 부족 시 "요청 색 먼저 + 유사상품 채우기").
        relaxed_color_family = params.get("p_color_family")
        relaxed = dict(params, p_subcategory=None, p_color_family=None)
        _strict_count = len(rows)
        logger.info(
            "[STEP 4.55][search] precision-filter relax retry — strict_count=%d (<%d) subcat=%r color=%r dropped",
            _strict_count,
            settings.SEARCH_FILTER_RELAX_MIN,
            params.get("p_subcategory"),
            params.get("p_color_family"),
        )
        # 색 필터가 relax 된 경우 신호를 전파 — 에이전트가 "요청 색 재고가 적어
        # 유사상품으로 채웠다"고 정직하게 안내하도록. subcategory 만 relax(색 없음)
        # 는 색 안내 대상이 아니므로 색이 있을 때만 채널을 세팅한다.
        if relaxed_color_family:
            color_relax_ctx.set(
                {
                    "requested_color": str(relaxed_color_family).strip().upper(),
                    "exact_count": _strict_count,
                    "subcategory_also_relaxed": params.get("p_subcategory") is not None,
                }
            )
        try:
            relaxed_rows = await _do_search(relaxed)
        except RpcContractError as exc:
            logger.error(
                "[STEP 4.55][search] relax retry contract drift -- keeping strict rows (row_index=%s exc=%s)",
                exc.row_index,
                type(exc).__name__,
            )
            relaxed_rows = []
        seen_ids = {r.get("id") for r in rows}
        rows = rows + [r for r in relaxed_rows if r.get("id") not in seen_ids]
        rows = rows[: settings.SEARCH_DEFAULT_K]

    state.raw_candidates = rows
    state.counts["raw"] = len(rows)

    # SPEC-BRAND-2TOWER-RESCORE — blend brand_multimodal_embeddings into
    # the product-distance ranking BEFORE personalize_rerank. Fail-open on
    # any error (RPC order preserved). α=1.0 → no-op (equivalent to off).
    if (
        settings.BRAND_2TOWER_ENABLED
        and rows
        and state.embedding is not None
        and 0.0 < settings.BRAND_2TOWER_ALPHA < 1.0
    ):
        try:
            before_top3 = [(r.get("brand"), float(r.get("distance", 1.0))) for r in rows[:3]]
            rescored, stats = _brand_2tower_rescore(rows, state.embedding, alpha=settings.BRAND_2TOWER_ALPHA)
            state.raw_candidates = rescored
            rows = rescored  # downstream personalize_rerank sees the rescored list
            after_top3 = [(r.get("brand"), float(r.get("distance", 1.0))) for r in rescored[:3]]
            logger.info(
                "[STEP 4.62][2tower] α=%.2f hit=%d miss=%d before=%s after=%s",
                settings.BRAND_2TOWER_ALPHA,
                stats["hit"],
                stats["miss"],
                before_top3,
                after_top3,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[STEP 4.62][2tower] skipped due to %s: %r", type(exc).__name__, exc)

    # SPEC-PERSONALIZE-RERANK — reorder raw rows by TasteProfile × brand
    # attributes BEFORE diversify_step so the brand/platform cap respects
    # the personalized order. Skipped when (a) flag off, (b) no user_key
    # (e.g., public /recommend), (c) no signal in the profile, (d) any
    # unexpected error (fail-open: keep RPC order).
    # 속성정렬 ("우와 비슷하다") 은 쿼리 의도라 익명/콜드 유저에도 적용.
    # v2.6 무드 — 예전엔 여기서 `_apply_mood_filter` 하드필터(해당 무드 상품만)를
    # 걸었으나, 0매치 fail-open 랜덤 + 근접무드 배제 + 필터 후 무드-blind 정렬로
    # 전환이 최악이었다(윤영 쿼리 지형도: 무드 6~11%). 하드필터를 제거하고
    # `_query_target_attrs` 의 mood 축으로 rerank 부스트만 준다 — 리콜 보존(상품
    # 더 노출)하면서 무드 매칭 상품을 상단으로. 2026-09-04.
    target_attrs = _query_target_attrs(req.item) if settings.ATTR_ALIGN_ENABLED else {}
    # 색 게이트가 relax 됐다면 요청 색을 소프트 부스트 축으로 추가 → exact-color 상단.
    if settings.ATTR_ALIGN_ENABLED and relaxed_color_family:
        target_attrs = {**target_attrs, "color": {str(relaxed_color_family).strip().upper()}}
    want_personalize = settings.PERSONALIZE_RERANK_ENABLED and bool(state.user_key)
    want_attr = bool(target_attrs)
    if rows and (want_personalize or want_attr):
        try:
            profile = None
            feature_scores = None
            exclude_axes = None
            if want_personalize:
                from app.infrastructure.memory.taste_profile import get_taste_store
                from app.scoring import feature_scores_cache

                profile = get_taste_store().get_or_create(state.user_key)
                # Visual-feature taste (ai.user_feature_scores) is only primed for the
                # app-auth'd search path; Telegram / internal /recommend read None.
                feature_scores = feature_scores_cache.get(state.user_key)
                exclude_axes = _query_pinned_axes(req.item) if feature_scores else None
            # 후보 feature_metadata 는 개인화(feature_scores) 또는 속성정렬 둘 중
            # 하나라도 필요하면 한 번에 붙인다(1 batch query).
            if feature_scores or want_attr:
                await _attach_feature_metadata(rows)
            weights = RerankWeights(
                liked_brand=settings.PERSONALIZE_LIKED_BRAND_W,
                disliked_brand=settings.PERSONALIZE_DISLIKED_BRAND_W,
                keyword=settings.PERSONALIZE_KEYWORD_W,
                price_fit=settings.PERSONALIZE_PRICE_FIT_W,
                gender_mismatch=settings.PERSONALIZE_GENDER_MISMATCH_W,
                feature=settings.PERSONALIZE_FEATURE_W,
                attr_fit=settings.ATTR_ALIGN_FIT_W if want_attr else 0.0,
                attr_material=settings.ATTR_ALIGN_MATERIAL_W if want_attr else 0.0,
                attr_color=settings.ATTR_ALIGN_COLOR_W if want_attr else 0.0,
                attr_pattern=settings.ATTR_ALIGN_PATTERN_W if want_attr else 0.0,
                attr_neckline=settings.ATTR_ALIGN_NECKLINE_W if want_attr else 0.0,
                attr_mood=settings.ATTR_ALIGN_MOOD_W if want_attr else 0.0,
                attr_length=settings.ATTR_ALIGN_LENGTH_W if want_attr else 0.0,
                attr_sleeve_length=settings.ATTR_ALIGN_SLEEVE_W if want_attr else 0.0,
                attr_leg_shape=settings.ATTR_ALIGN_LEG_SHAPE_W if want_attr else 0.0,
                attr_surface=settings.ATTR_ALIGN_SURFACE_W if want_attr else 0.0,
                attr_texture=settings.ATTR_ALIGN_TEXTURE_W if want_attr else 0.0,
                attr_design_details=settings.ATTR_ALIGN_DESIGN_DETAILS_W if want_attr else 0.0,
                attr_nonapparel=settings.ATTR_ALIGN_NONAPPAREL_W if want_attr else 0.0,
                attr_wash=settings.ATTR_ALIGN_WASH_W if want_attr else 0.0,
                attr_graphics=settings.ATTR_ALIGN_GRAPHICS_W if want_attr else 0.0,
            )
            if exclude_axes:
                logger.info(
                    "[STEP 4.65][rerank] adaptive-α: query pinned axes=%s → excluded from feature match",
                    sorted(exclude_axes),
                )
            if want_attr:
                logger.info("[STEP 4.65][rerank] 속성정렬 target=%s", {k: sorted(v) for k, v in target_attrs.items()})
            before_top3 = [(r.get("brand"), float(r.get("distance", 1.0))) for r in rows[:3]]
            state.raw_candidates = _personalize_rerank(
                rows,
                profile,
                weights=weights,
                feature_scores=feature_scores,
                exclude_axes=exclude_axes,
                target_attrs=target_attrs,
            )
            after_top3 = [(r.get("brand"), float(r.get("distance", 1.0))) for r in state.raw_candidates[:3]]
            if before_top3 != after_top3:
                logger.info("[STEP 4.65][rerank] reorder applied before=%s after=%s", before_top3, after_top3)
            else:
                logger.info("[STEP 4.65][rerank] no reorder (top3 unchanged) user_key=%s", state.user_key)
        except Exception as exc:  # noqa: BLE001 — never break search
            logger.warning("[STEP 4.65][rerank] skipped due to %s: %r", type(exc).__name__, exc)

    # 결과 분포 — top-5 distance / degraded / 브랜드/플랫폼/subcategory (v6:
    # RPC 가 distance ASC 로 이미 정렬해 반환 — min=best).
    if rows:
        logger.info("[STEP 4.6][search] RPC 응답 — raw_count=%d (v6 embedding-first, distance ASC)", len(rows))
        for i, r in enumerate(rows[:5]):
            logger.info(
                "[STEP 4.6][search]   #%d id=%s distance=%.4f degraded=%s brand=%s platform=%s subcat=%s name=%r",
                i + 1,
                r.get("id"),
                float(r.get("distance", 1.0)),
                r.get("degraded"),
                r.get("brand"),
                r.get("platform"),
                r.get("subcategory"),
                (r.get("name") or "")[:60],
            )
        # distance 통계 — min/median/max (ASC 정렬이므로 min=best)
        distances = [float(r.get("distance", 1.0)) for r in rows]
        distances_sorted = sorted(distances)
        logger.info(
            "[STEP 4.6][search] distance_dist min=%.4f median=%.4f max=%.4f (ASC → min=best)",
            distances_sorted[0],
            distances_sorted[len(distances_sorted) // 2],
            distances_sorted[-1],
        )
        # degraded(스타일노드 필터 드롭 → 카테고리-only 폴백) 행 수 — 관측용
        degraded_count = sum(1 for r in rows if r.get("degraded"))
        logger.info("[STEP 4.6][search] degraded_count=%d (style-node filter dropped → category-only)", degraded_count)
    else:
        logger.warning("[STEP 4.6][search] ⚠️ raw_count=0 — 0결과! (v6 embedding-first 검색 풀 비어있음)")

    state.end("search")
    return state
