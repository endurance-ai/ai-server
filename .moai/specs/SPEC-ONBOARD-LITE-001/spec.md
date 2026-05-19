---
id: SPEC-ONBOARD-LITE-001
version: 1.0.0
status: draft
created: 2026-05-19
updated: 2026-05-19
author: hchsa77@gmail.com
priority: P0
supersedes: SPEC-ONBOARD-CARDS-001
amends: SPEC-AGENT-V2-REACT
---

# SPEC-ONBOARD-LITE-001 — 온보딩 friction 제거 (경량 first-touch)

## 배경

진단(설계 문서 §1): 온보딩 카드 선택 → `liked_keywords` → ReAct LLM 컨텍스트
텍스트 1줄이 유일 소비처. `search_service` / `search_repository` /
`pipeline.search` 에 taste 참조 0건 → 추천 랭킹 0% 반영. 동시에
`onboarding_required` 가 신규 유저 첫 메시지를 카드 퍼널로 강제 진입시켜
핵심 가치 체험을 지연. 결론: 높은 friction · zero payoff.

설계 문서: `docs/superpowers/specs/2026-05-19-onboarding-friction-removal-design.md`

## 범위

- IN: 온보딩 카드 서브그래프 완전 제거 + 경량 first-touch 대체.
- OUT (별도 SPEC): 취향(TasteProfile) → v6 검색 랭킹 반영("루프 닫기").
  본 SPEC 이후에도 취향은 검색 랭킹 미반영 상태로 의도적으로 유지.

## 요구사항 (EARS)

- REQ-OBL-001: WHEN 신규 유저(`sess.onboarded_at IS NULL`)가 actionable
  메시지(photo OR url OR `/start` 아닌 비어있지 않은 text)를 보내면,
  시스템은 `ingest` 인라인으로 1줄 그리팅을 발송하고 `onboarded_at`을
  현재 시각으로 마킹한 뒤 같은 턴에 정상 추천 경로(resolve_image / agent /
  pick_item)로 진행한다.
- REQ-OBL-002: WHEN 신규 유저가 `/start`-only(photo/url/callback 없음)
  메시지를 보내면, 시스템은 `intro` 노드로 라우팅해 1회성 서비스 소개를
  발송하고 `onboarded_at`을 마킹한 뒤 턴을 종료한다.
- REQ-OBL-003: WHEN 임의 유저가 reset 키워드(`/reset`, `취향 초기화`,
  `reset taste`)를 보내면, 시스템은 호출자 TasteProfile을 삭제하고 ack를
  발송한 뒤 턴을 종료한다(`__end__`).
- REQ-OBL-004: 시스템은 온보딩 카드 서브그래프(노드/라우팅 술어/카드
  빌더/Pinterest 대량 스크랩/`seed_from_onboarding`)를 포함하지 않는다.
- REQ-OBL-005: Session PG 컬럼(`onboard_stage` 등 6개)은 물리적으로
  존치하되(파괴적 migration 없음) 코드에서 read/write 하지 않는다.
  `onboarded_at` 만 first-touch 판별용으로 유지한다.
- REQ-OBL-006: 핀 링크 → 추천 핵심 흐름(`link_resolver` 경유)은 회귀 없이
  유지된다(Apify 보드/프로필 대량 스크랩만 제거; 핀 링크 추천은 불변).

## 검증

- `tests/test_graph_nodes/test_route_after_ingest.py` (라우팅 7),
  `tests/test_graph_nodes/test_first_touch.py` (first-touch 5),
  `tests/test_reset_keywords.py` (3).
- 전체 `uv run pytest` 그린 + `ruff check`/`format` clean.
- 삭제 모듈 import 잔존 0 (grep 스윕).

## 코드 위치

| 개념 | 위치 |
|------|------|
| 라우팅 진입 분기 | `app/graphs/fashion_bot.py::_route_after_ingest_v2` |
| first-touch 인라인 | `app/graphs/nodes/_first_touch.py::maybe_first_touch` (ingest 호출) |
| non-actionable 인트로 | `app/graphs/nodes/intro.py` |
| reset 키워드 단일 소스 | `app/channels/reset_keywords.py` |
| 신규 유저 판별 필드 | `app/infrastructure/memory/session.py::Session.onboarded_at` |
