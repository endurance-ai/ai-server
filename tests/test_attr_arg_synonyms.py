"""에이전트 속성 인자 표기 → 카탈로그 어휘 (2026-10-18).

dev 9/1~10/2 search_products 에서 인자 값이 카탈로그 어휘와 달라 가산이 안 걸린 호출:
neckline 9건 중 7건(crew-neck 4 등), leg_shape 11건 중 6건(bootcut 6), sleeve_length 28건 중
4건(three-quarter 4), design_details 13건 중 10건, material lace 5건(texture 어휘).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.search_service import _query_target_attrs


def _item(**kw) -> SimpleNamespace:
    base = {"search_query": "", "search_query_ko": None}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("crew-neck", {"crew"}),
        ("Crew Neck", {"crew"}),
        ("scoop-neck", {"scoop"}),
        ("high-neck", {"mock", "turtleneck"}),
        ("notch-collar", {"collared"}),
        ("off_shoulder", {"off-shoulder"}),
        ("v-neck", {"v-neck"}),
    ],
)
def test_neckline_arg_maps_to_catalog(raw, expected):
    assert _query_target_attrs(_item(neckline=raw))["neckline"] == expected


def test_sleeve_three_quarter_hyphen():
    assert _query_target_attrs(_item(sleeve_length="three-quarter"))["sleeve_length"] == {"three_quarter"}


def test_leg_bootcut_to_flare():
    assert _query_target_attrs(_item(leg_shape="bootcut"))["leg_shape"] == {"flare"}
    assert _query_target_attrs(_item(leg_shape="wide"))["leg_shape"] == {"wide"}


@pytest.mark.parametrize(("raw", "expected"), [("button-front", {"buttoned"}), ("shirring", {"ruched"})])
def test_design_details_synonyms(raw, expected):
    assert _query_target_attrs(_item(design_details=raw))["design_details"] == expected


def test_lace_material_goes_to_texture():
    out = _query_target_attrs(_item(fabric="lace"))
    assert "lace" in out["texture"]
    assert "lace" not in out.get("material", set())


def test_distressed_design_goes_to_texture():
    out = _query_target_attrs(_item(design_details="distressed"))
    assert "distressed" in out["texture"]
    assert "design_details" not in out


def test_unknown_value_kept_as_is():
    assert _query_target_attrs(_item(design_details="raglan"))["design_details"] == {"raglan"}
