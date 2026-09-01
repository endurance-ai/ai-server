"""v26 ↔ v1.1 feature_metadata 머지 로직(_merge_v26_attrs) 단위 검증.

누수 픽스: v1.1 row 없는 v26-only 상품이 material/pattern/color/neckline rerank 를
못 받던 문제. fill 축은 v1.1 우선·빈 곳만 채움(220k 거동 불변), neckline 은 vocab 정규화.
"""

from __future__ import annotations

from app.services.search_service import _merge_v26_attrs


def _v26(**over):
    base = {
        "length": None,
        "sleeve_length": None,
        "leg_shape": None,
        "surface": None,
        "texture": None,
        "design_details": None,
        "heel_type": None,
        "heel_height": None,
        "shaft": None,
        "shoe_toe": None,
        "bag_size": None,
        "bag_structure": None,
        "frame_shape": None,
        "metal_tone": None,
        "material": None,
        "pattern": None,
        "primary_color": None,
        "neckline": None,
    }
    base.update(over)
    return base


def test_v26_only_fills_overlap_axes_and_normalizes_neckline():
    # v1.1 row 없음(fm=None) → material/pattern/color 채우고 neckline 정규화(v→v-neck).
    out = _merge_v26_attrs(
        None, _v26(material=["cotton"], pattern="striped", primary_color="NAVY", neckline="v", surface="matte")
    )
    assert out["material"] == ["cotton"]
    assert out["pattern"] == "striped"
    assert out["primary_color"] == "NAVY"
    assert out["neckline"] == "v-neck"  # 정규화됨
    assert out["surface"] == "matte"  # 신규축 override


def test_v11_present_fill_keys_not_overridden():
    # v1.1 이 material/pattern/neckline 가짐 → v26 로 덮지 않음(거동 불변). 신규축만 추가.
    fm = {"material": ["wool"], "pattern": "solid", "neckline": "crew", "fit": "slim"}
    out = _merge_v26_attrs(fm, _v26(material=["cotton"], pattern="striped", neckline="v", heel_type="stiletto"))
    assert out["material"] == ["wool"]  # v1.1 유지
    assert out["pattern"] == "solid"  # v1.1 유지
    assert out["neckline"] == "crew"  # v1.1 유지
    assert out["fit"] == "slim"  # v1.1 보존
    assert out["heel_type"] == "stiletto"  # 신규축 추가


def test_neckline_unmappable_is_skipped():
    # v26 neckline 이 v1.1 vocab 에 매핑 안 되면(cowl 등) 채우지 않음.
    out = _merge_v26_attrs(None, _v26(neckline="cowl", material=["silk"]))
    assert "neckline" not in out
    assert out["material"] == ["silk"]


def test_neckline_mappings():
    for v26_val, v11_val in [
        ("v", "v-neck"),
        ("round", "crew"),
        ("off_shoulder", "off-shoulder"),
        ("collar", "collared"),
    ]:
        out = _merge_v26_attrs(None, _v26(neckline=v26_val))
        assert out["neckline"] == v11_val


def test_empty_v26_returns_fm_unchanged():
    fm = {"material": ["denim"]}
    assert _merge_v26_attrs(fm, _v26()) is fm  # 전부 None → fm 그대로
    assert _merge_v26_attrs(None, None) is None
