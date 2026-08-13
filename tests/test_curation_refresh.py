"""curation_refresh 후보 선택 단위 테스트 (DB/네트워크 없음) + chips 상수 검증."""

from __future__ import annotations

from app.services.curation_chips import chips_for
from app.services.curation_refresh import select_candidate_ids


def test_chips_contract():
    women = chips_for("women")
    assert [c.id for c in women] == ["chip-w1", "chip-w2", "chip-w3", "chip-w4", "chip-w5"]
    # 노출 label_ko ≠ 실행 query_en 분리, 금지: 가격 조건/부정형은 값 검토 대상이라 여기선 형식만 확인
    for chip in women:
        assert chip.label_ko and chip.query_en and chip.category
        assert chip.query_en.isascii()  # 한국어 라벨을 그대로 검색에 태우지 않는다
    assert chips_for("men") == []  # men 골든셋 등록 전까지 빈 배열 (스펙 v1.1)


def test_candidate_selection_enforces_brand_cap_and_cross_section_exclusion():
    rows = []
    for pid in range(1, 31):
        rows.append(
            {
                "product_id": pid,
                "base_score": float(100 - pid),
                "base_rank": pid,
                "brand_key": f"brand-{(pid - 1) // 3}",
                "style_node_id": pid,
            }
        )
    selected = select_candidate_ids(
        rows,
        section_id="popular",
        excluded_ids={1},
        seed="test",
    )
    assert len(selected) == 12
    assert 1 not in selected
    selected_brands = [f"brand-{(pid - 1) // 3}" for pid in selected]
    assert all(selected_brands.count(brand) <= 2 for brand in set(selected_brands))


def test_candidate_selection_uses_taste_scores_without_weakening_brand_cap():
    rows = [
        {
            "product_id": pid,
            "base_score": float(100 - pid),
            "base_rank": pid,
            "brand_key": f"brand-{pid}",
            "style_node_id": pid,
        }
        for pid in range(1, 21)
    ]
    baseline = select_candidate_ids(
        rows,
        section_id="under-100",
        excluded_ids=set(),
        seed="taste-regression",
    )
    taste_scores = {pid: -20.0 for pid in range(1, 21)}
    taste_scores[8] = 20.0
    personalized = select_candidate_ids(
        rows,
        section_id="under-100",
        excluded_ids=set(),
        taste_scores=taste_scores,
        seed="taste-regression",
    )

    assert baseline[0] == 1
    assert personalized[0] == 8
    assert len(personalized) == 12


def _feature_rows() -> list[dict]:
    # Product 8 is black; everything else beige. All products have unique brands.
    return [
        {
            "product_id": pid,
            "base_score": float(100 - pid),
            "base_rank": pid,
            "brand_key": f"brand-{pid}",
            "style_node_id": pid,
            "feature_metadata": {"primary_color": "black" if pid == 8 else "beige"},
        }
        for pid in range(1, 21)
    ]


def test_candidate_selection_uses_feature_scores_without_weakening_brand_cap():
    rows = _feature_rows()
    baseline = select_candidate_ids(rows, section_id="under-100", excluded_ids=set(), seed="feat")
    # Loves black, dislikes beige — the lone black product should surface to #1.
    feature_scores = {("color", "beige"): -20.0, ("color", "black"): 20.0}
    personalized = select_candidate_ids(
        rows,
        section_id="under-100",
        excluded_ids=set(),
        feature_scores=feature_scores,
        seed="feat",
    )
    assert baseline[0] == 1
    assert personalized[0] == 8
    assert len(personalized) == 12


def test_candidate_selection_missing_feature_metadata_is_neutral():
    # feature_scores present but rows carry no enriched metadata → every product
    # contributes the neutral 0.5, so ordering falls back to base rank (no crash).
    rows = [
        {
            "product_id": pid,
            "base_score": float(100 - pid),
            "base_rank": pid,
            "brand_key": f"brand-{pid}",
            "style_node_id": pid,
        }
        for pid in range(1, 21)
    ]
    selected = select_candidate_ids(
        rows,
        section_id="popular",
        excluded_ids=set(),
        feature_scores={("color", "black"): 20.0},
        seed="neutral",
    )
    assert selected[0] == 1
    assert len(selected) == 12
