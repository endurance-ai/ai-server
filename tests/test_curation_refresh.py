"""curation_refresh 순수 파서 단위 테스트 (DB/네트워크 없음) + chips 상수 검증."""

from __future__ import annotations

from collections import Counter

from app.services.curation_chips import chips_for
from app.services.curation_refresh import (
    _parse_notion_page,
    _parse_product_ids,
    select_candidate_ids,
)


def _page(props: dict) -> dict:
    return {"id": "abcd1234-5678-90ab-cdef-1234567890ab", "properties": props}


def _title(text: str) -> dict:
    return {"title": [{"plain_text": text}]}


def _rich(text: str) -> dict:
    return {"rich_text": [{"plain_text": text}]}


def test_parse_product_ids_tolerates_separators():
    assert _parse_product_ids("123, 456 789;1011") == [123, 456, 789, 1011]
    assert _parse_product_ids("") == []


def test_parse_notion_page_full():
    parsed = _parse_notion_page(
        _page(
            {
                "구좌명": _title("지금 뜨는 베트남 핫걸 ST"),
                "구좌 ID": _rich("editorial-vietnam-hotgirl"),
                "slot_type": {"select": {"name": "editorial"}},
                "서브타이틀": _rich("사이공 트렌드세터의 여름 무드"),
                "상품": _rich("11, 22, 33"),
                "순서": {"number": 4},
                "활성": {"checkbox": True},
                "gender_scope": {"multi_select": [{"name": "women"}]},
            }
        )
    )
    assert parsed is not None
    assert parsed["section_id"] == "editorial-vietnam-hotgirl"
    assert parsed["title"] == "지금 뜨는 베트남 핫걸 ST"
    assert parsed["subtitle"] == "사이공 트렌드세터의 여름 무드"
    assert parsed["product_ids"] == [11, 22, 33]
    assert parsed["sort_order"] == 4
    assert parsed["is_active"] is True
    assert parsed["genders"] == ["women"]


def test_parse_notion_page_defaults_and_missing_title():
    parsed = _parse_notion_page(
        _page(
            {
                "구좌명": _title("지금 인기 브랜드"),
                "구좌 ID": _rich("popular"),
                "slot_type": {"select": {"name": "auto"}},
                "gender_scope": {"multi_select": [{"name": "women"}, {"name": "men"}]},
                "활성": {"checkbox": False},
            }
        )
    )
    assert parsed is not None
    assert parsed["genders"] == ["women", "men"]
    assert parsed["sort_order"] == 100
    assert parsed["is_active"] is False
    assert parsed["product_ids"] == []
    # Required operating fields are fail-closed.
    assert _parse_notion_page(_page({"서브타이틀": _rich("x")})) is None
    assert _parse_notion_page(_page({"구좌명": _title("missing id")})) is None


def test_parse_legacy_auto_without_gender_scope_as_both_genders():
    parsed = _parse_notion_page(
        _page(
            {
                "구좌명": _title("Under $100"),
                "구좌 ID": _rich("under-100"),
                "slot_type": {"select": {"name": "auto"}},
                "활성": {"checkbox": True},
            }
        )
    )
    assert parsed is not None
    assert parsed["genders"] == ["women", "men"]


def test_chips_contract():
    women = chips_for("women")
    assert [c.id for c in women] == ["chip-w1", "chip-w2", "chip-w3", "chip-w4", "chip-w5"]
    # 노출 label_ko ≠ 실행 query_en 분리, 금지: 가격 조건/부정형은 값 검토 대상이라 여기선 형식만 확인
    for chip in women:
        assert chip.label_ko and chip.query_en and chip.category
        assert chip.query_en.isascii()  # 한국어 라벨을 그대로 검색에 태우지 않는다
    assert chips_for("men") == []  # men 골든셋 등록 전까지 빈 배열 (스펙 v1.1)


def test_candidate_selection_enforces_hot_overall_brand_and_cross_section_quotas():
    rows = []
    for pid in range(1, 21):
        rows.append(
            {
                "product_id": pid,
                "is_hot": pid <= 10,
                "base_score": float(100 - pid),
                "base_rank": pid if pid <= 10 else pid - 10,
                "brand_key": f"brand-{(pid - 1) // 2}",
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
    assert len([pid for pid in selected if pid <= 10]) == 8
    assert len([pid for pid in selected if pid > 10]) == 4
    assert 1 not in selected


def test_feed_brand_cap_limits_one_brand_across_the_whole_feed():
    """교차 섹션 캡: 한 브랜드가 세 구좌를 도배하지 못하도록 피드 전역 제한.

    상위 hot 2개가 같은 브랜드('dom'). 나머지 재고는 브랜드가 전부 달라 구좌를
    채우기 충분하다. 캡이 없으면 'dom'은 구좌마다 섹션 캡(2)까지 잡혀 총 6회
    등장하지만, feed_cap=2 면 첫 구좌에서만 2회 잡히고 이후 구좌에선 배제된다.
    """

    # 실데이터처럼 브랜드가 넉넉한 풀(섹션당 hot~50종). 상위 hot 2개만 'dom'.
    def pool() -> list[dict]:
        rows: list[dict] = []
        for pid in range(1, 41):  # hot 후보 40개
            rows.append(
                {
                    "product_id": pid,
                    "is_hot": True,
                    "base_score": float(1000 - pid),
                    "base_rank": pid,
                    "brand_key": "dom" if pid <= 2 else f"hot-{pid}",
                    "style_node_id": pid,
                }
            )
        for pid in range(41, 81):  # overall 후보 40개
            rows.append(
                {
                    "product_id": pid,
                    "is_hot": False,
                    "base_score": float(1000 - pid),
                    "base_rank": pid - 40,
                    "brand_key": f"over-{pid}",
                    "style_node_id": pid,
                }
            )
        return rows

    feed_brands: Counter[str] = Counter()
    picks = []
    for section_id in ("popular", "trending-search", "under-100"):
        selected = select_candidate_ids(
            pool(),
            section_id=section_id,
            excluded_ids=set(),
            seed=section_id,
            feed_brands=feed_brands,
            feed_cap=2,
        )
        assert len(selected) == 12  # 캡이 걸려도 구좌는 여전히 꽉 찬다(언더필 없음)
        picks.append(selected)

    dom_total = sum(1 for sel in picks for pid in sel if pid in (1, 2))
    assert dom_total == 2  # 피드 전체에서 최대 2회
    assert feed_brands["dom"] == 2


def test_feed_brand_cap_counter_untouched_when_section_discarded():
    """require_full 로 폐기된 구좌는 공유 카운터를 오염시키지 않는다."""
    # hot 재고가 8 미만 → require_full=True 는 [] 를 돌려주고, 그 사이 잠깐
    # 담겼던 브랜드가 feed_brands 에 새어 들어가면 안 된다.
    rows = [
        {
            "product_id": pid,
            "is_hot": True,
            "base_score": float(100 - pid),
            "base_rank": pid,
            "brand_key": "dom",
            "style_node_id": pid,
        }
        for pid in range(1, 4)  # hot 3개뿐(브랜드도 하나) → 8 채우기 불가
    ]
    feed_brands: Counter[str] = Counter()
    selected = select_candidate_ids(
        rows,
        section_id="popular",
        excluded_ids=set(),
        seed="discard",
        feed_brands=feed_brands,
        feed_cap=2,
    )
    assert selected == []
    assert feed_brands == Counter()  # 폐기된 구좌는 카운터 미반영


def test_candidate_selection_uses_taste_scores_without_weakening_quotas():
    rows = [
        {
            "product_id": pid,
            "is_hot": pid <= 10,
            "base_score": float(100 - pid),
            "base_rank": pid if pid <= 10 else pid - 10,
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
    assert len([pid for pid in personalized if pid <= 10]) == 8
    assert len([pid for pid in personalized if pid > 10]) == 4


def _feature_rows() -> list[dict]:
    # Product 8 is black; everything else beige. All hot, unique brands.
    return [
        {
            "product_id": pid,
            "is_hot": pid <= 10,
            "base_score": float(100 - pid),
            "base_rank": pid if pid <= 10 else pid - 10,
            "brand_key": f"brand-{pid}",
            "style_node_id": pid,
            "feature_metadata": {"primary_color": "black" if pid == 8 else "beige"},
        }
        for pid in range(1, 21)
    ]


def test_candidate_selection_uses_feature_scores_without_weakening_quotas():
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
    assert len([pid for pid in personalized if pid <= 10]) == 8
    assert len([pid for pid in personalized if pid > 10]) == 4


def test_candidate_selection_missing_feature_metadata_is_neutral():
    # feature_scores present but rows carry no enriched metadata → every product
    # contributes the neutral 0.5, so ordering falls back to base rank (no crash).
    rows = [
        {
            "product_id": pid,
            "is_hot": pid <= 10,
            "base_score": float(100 - pid),
            "base_rank": pid if pid <= 10 else pid - 10,
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
