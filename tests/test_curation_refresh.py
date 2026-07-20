"""curation_refresh 순수 파서 단위 테스트 (DB/네트워크 없음) + chips 상수 검증."""

from __future__ import annotations

from app.services.curation_chips import chips_for
from app.services.curation_refresh import _parse_notion_page, _parse_product_ids


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
                "서브타이틀": _rich("사이공 트렌드세터의 여름 무드"),
                "상품": _rich("11, 22, 33"),
                "순서": {"number": 4},
                "활성": {"checkbox": True},
                "성별": {"select": {"name": "women"}},
            }
        )
    )
    assert parsed is not None
    assert parsed["section_id"] == "editorial-abcd12345678"
    assert parsed["title"] == "지금 뜨는 베트남 핫걸 ST"
    assert parsed["subtitle"] == "사이공 트렌드세터의 여름 무드"
    assert parsed["product_ids"] == [11, 22, 33]
    assert parsed["sort_order"] == 4
    assert parsed["is_active"] is True
    assert parsed["genders"] == ["women"]


def test_parse_notion_page_defaults_and_missing_title():
    # 성별 미지정 → 양쪽 노출, 순서 미지정 → 100
    parsed = _parse_notion_page(_page({"구좌명": _title("가을 무드"), "활성": {"checkbox": False}}))
    assert parsed is not None
    assert parsed["genders"] == ["women", "men"]
    assert parsed["sort_order"] == 100
    assert parsed["is_active"] is False
    assert parsed["product_ids"] == []
    # 구좌명 없는 행은 skip
    assert _parse_notion_page(_page({"서브타이틀": _rich("x")})) is None


def test_chips_contract():
    women = chips_for("women")
    assert [c.id for c in women] == ["chip-w1", "chip-w2", "chip-w3", "chip-w4", "chip-w5"]
    # 노출 label_ko ≠ 실행 query_en 분리, 금지: 가격 조건/부정형은 값 검토 대상이라 여기선 형식만 확인
    for chip in women:
        assert chip.label_ko and chip.query_en and chip.category
        assert chip.query_en.isascii()  # 한국어 라벨을 그대로 검색에 태우지 않는다
    assert chips_for("men") == []  # men 골든셋 등록 전까지 빈 배열 (스펙 v1.1)
