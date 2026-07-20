"""유도 칩(chips) 단일 소스 — GET /v1/curation 의 `chips[]` 공급.

골든셋 정책 (2026-07-14 확정):
  - 등록 가능 값 = 골든셋 통과 값만. 신규 값은 run_goldenset.py 배치 검증 후 추가.
  - 여성 = 문형 채택 방식 (무드/소재/핏/컬러 문형 내 값 교체), 남성 = 값 화이트리스트 방식.
  - 노출은 label_ko, 실행은 검증된 query_en — 한국어 라벨을 그대로 검색에 태우지 않는다.
  - 금지: 가격 조건(v6 RPC에 가격 필터 없음 — 100% 실패), 부정형, 성별별 블랙리스트 값,
    TPO(여성만 배제 — 남성은 S값 허용).
  - 배열 순서 = 노출 순서. 교체 = 이 파일 수정 + 서버 배포 (앱 배포 불필요).

이 모듈은 순수 상수 + 조회 함수만 — DB/네트워크 접근 없음.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Gender = Literal["women", "men"]


class Chip(BaseModel):
    id: str
    pattern: str  # mood | aesthetic | fit | ... (골든셋 문형/축 분류)
    label_ko: str
    query_en: str
    category: str


# 여성 5종 — 스펙 JSON 계약 그대로 (2026-07-14 골든셋 최종)
_WOMEN_CHIPS: tuple[Chip, ...] = (
    Chip(id="chip-w1", pattern="mood", label_ko="유니크한 미니백", query_en="quirky unique mini bag", category="bag"),
    Chip(id="chip-w2", pattern="aesthetic", label_ko="Y2K 스타일 탑", query_en="y2k top", category="top"),
    Chip(id="chip-w3", pattern="fit", label_ko="카프리 팬츠", query_en="capri pants", category="pants"),
    Chip(id="chip-w4", pattern="fit", label_ko="로우라이즈 진", query_en="low rise jeans", category="jeans"),
    Chip(id="chip-w5", pattern="mood", label_ko="로맨틱한 원피스", query_en="romantic dress", category="dress"),
)

# 남성 4종 — 셀 확정(2026-07-14, 현규 실검색 판정 S). 단, v1.1 계약상 men 칩은
# men 골든셋(query_en 확정) 등록 전까지 빈 배열이어야 하므로 기본 비활성.
# 활성화 절차: 골든셋 통과한 query_en으로 아래 값 확정 → _MEN_CHIPS_ACTIVE = True → 배포.
_MEN_CHIPS_ACTIVE = False
_MEN_CHIPS: tuple[Chip, ...] = (
    Chip(id="chip-m1", pattern="fit", label_ko="크롭 반팔티", query_en="cropped short sleeve t-shirt", category="top"),
    Chip(
        id="chip-m2",
        pattern="aesthetic",
        label_ko="카모 패턴 카고 팬츠",
        query_en="camo pattern cargo pants",
        category="pants",
    ),
    Chip(id="chip-m3", pattern="fit", label_ko="루즈핏 데님 팬츠", query_en="loose fit denim pants", category="jeans"),
    Chip(
        id="chip-m4",
        pattern="aesthetic",
        label_ko="여름 인디 밴드 티셔츠",
        query_en="summer indie band t-shirt",
        category="top",
    ),
)


def chips_for(gender: Gender) -> list[Chip]:
    """gender별 노출 칩. men은 골든셋 등록 전까지 빈 배열 (스펙 v1.1)."""
    if gender == "women":
        return list(_WOMEN_CHIPS)
    return list(_MEN_CHIPS) if _MEN_CHIPS_ACTIVE else []
