"""쿼리 텍스트 → target 속성 추출 (search_service._query_target_attrs).

에이전트가 fit/fabric 을 구조화 인자로 안 넘기고 free-text 쿼리에만 담는
실측 패턴을 보완해, 잠들어 있던 속성정렬 rerank(attr_fit/attr_material)가
켜지도록 하는 배선의 회귀 테스트.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.search_service import (
    _extract_fit_from_text,
    _extract_material_from_text,
    _extract_pattern_from_text,
    _query_target_attrs,
)


def _item(
    search_query: str = "",
    *,
    fit=None,
    fabric=None,
    pattern=None,
    neckline=None,
    length=None,
    sleeve_length=None,
    leg_shape=None,
    search_query_ko=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        search_query=search_query,
        search_query_ko=search_query_ko,
        fit=fit,
        fabric=fabric,
        pattern=pattern,
        neckline=neckline,
        length=length,
        sleeve_length=sleeve_length,
        leg_shape=leg_shape,
    )


# ── fit 추출 ────────────────────────────────────────────────────────────────


def test_extract_fit_english_word_boundary():
    assert _extract_fit_from_text("oversized hoodie sweatpants women") == {"oversized"}
    assert _extract_fit_from_text("slim fit jeans") == {"slim", "skinny"}


def test_extract_fit_multiword_hyphen_variants():
    # "wide-leg" / "wide leg" / "wideleg" 모두 relaxed 로.
    assert _extract_fit_from_text("wide leg trousers") == {"relaxed"}
    assert _extract_fit_from_text("wide-leg trousers") == {"relaxed"}


def test_extract_fit_boxy_maps_to_boxy_value():
    # feature_metadata.fit 에 'boxy' 값이 존재 → 쿼리 boxy 가 boxy 후보와 매칭돼야.
    assert "boxy" in _extract_fit_from_text("boxy tee")


def test_extract_fit_korean():
    assert _extract_fit_from_text("오버핏 후드") == {"oversized"}
    assert _extract_fit_from_text("슬림한 청바지") == {"slim", "skinny"}


def test_extract_fit_none_when_absent():
    assert _extract_fit_from_text("elegant midi dress") == set()
    assert _extract_fit_from_text("") == set()


# ── material 추출 ───────────────────────────────────────────────────────────


def test_extract_material_english():
    assert _extract_material_from_text("black leather jacket") == {"leather"}
    assert _extract_material_from_text("raw denim") == {"denim"}


def test_extract_material_canonicalizes_variants():
    assert _extract_material_from_text("knitted sweater") == {"knit"}
    assert _extract_material_from_text("gore-tex shell") == {"gore-tex"}


def test_extract_material_korean_multichar_only():
    assert _extract_material_from_text("가죽 자켓") == {"leather"}
    assert _extract_material_from_text("데님 셔츠") == {"denim"}


def test_extract_material_none_when_absent():
    assert _extract_material_from_text("elegant midi dress") == set()


# ── pattern 추출 ────────────────────────────────────────────────────────────


def test_extract_pattern_english():
    assert _extract_pattern_from_text("striped shirt") == {"striped"}
    assert _extract_pattern_from_text("plaid flannel") == {"checked"}
    assert _extract_pattern_from_text("leopard print coat") == {"animal"}


def test_extract_pattern_korean():
    assert _extract_pattern_from_text("체크 셔츠") == {"checked"}
    assert _extract_pattern_from_text("꽃무늬 원피스") == {"floral"}


def test_extract_pattern_excludes_solid():
    # solid 는 카탈로그 기본값이라 매핑에서 제외 → 추출 안 됨.
    assert _extract_pattern_from_text("solid black tee") == set()


def test_extract_pattern_none_when_absent():
    assert _extract_pattern_from_text("elegant midi dress") == set()


# ── _query_target_attrs 통합 ─────────────────────────────────────────────────


def test_target_attrs_from_free_text_fires_both_axes():
    out = _query_target_attrs(_item("oversized leather jacket"))
    assert out["fit"] == {"oversized"}
    assert out["material"] == {"leather"}


def test_target_attrs_fires_pattern_axis():
    out = _query_target_attrs(_item("striped cotton shirt"))
    assert out["pattern"] == {"striped"}
    assert out["material"] == {"cotton"}


def test_target_attrs_structured_fit_takes_precedence():
    # 구조화 fit 이 있으면 텍스트 추출을 덮어쓴다(우선).
    out = _query_target_attrs(_item("slim jeans", fit="oversized"))
    assert out["fit"] == {"oversized"}


def test_target_attrs_material_unions_structured_and_text():
    out = _query_target_attrs(_item("wool coat", fabric="cashmere"))
    assert out["material"] == {"cashmere", "wool"}


def test_target_attrs_empty_for_attributeless_query():
    assert _query_target_attrs(_item("midi dress")) == {}


def test_target_attrs_reads_korean_query_field():
    out = _query_target_attrs(_item("", search_query_ko="오버핏 니트"))
    assert out["fit"] == {"oversized"}
    assert out["material"] == {"knit"}


# ── 명시 속성 arg (2026-08-31: material→fabric / pattern / neckline) ──────────


def test_target_attrs_structured_pattern_unions_with_text():
    # 명시 pattern arg 가 텍스트 추출과 합집합. (텍스트 없이 arg 만으로도 발동.)
    out = _query_target_attrs(_item("cotton shirt", pattern="striped"))
    assert out["pattern"] == {"striped"}
    assert out["material"] == {"cotton"}


def test_target_attrs_structured_neckline_arg_only():
    # neckline 은 텍스트 추출기가 없어 명시 arg 로만 target 세팅된다.
    out = _query_target_attrs(_item("knit sweater", neckline="v-neck"))
    assert out["neckline"] == {"v-neck"}
    assert out["material"] == {"knit"}
    # arg 없으면 텍스트에 넥라인 단어가 있어도 안 잡힘(추출기 부재).
    assert "neckline" not in _query_target_attrs(_item("v-neck knit sweater"))


def test_target_attrs_v26_axes_length_sleeve_leg():
    # v2.6 축(product_features_v26) — 명시 arg 로만 target 세팅.
    out = _query_target_attrs(_item("wide jeans", length="full", sleeve_length="long", leg_shape="wide"))
    assert out["length"] == {"full"}
    assert out["sleeve_length"] == {"long"}
    assert out["leg_shape"] == {"wide"}


def test_target_attrs_length_cropped_normalizes_to_crop():
    # length vocab 중복(crop/cropped) → 'cropped' 를 'crop' 으로 정규화.
    assert _query_target_attrs(_item("cropped pants", length="cropped"))["length"] == {"crop"}
