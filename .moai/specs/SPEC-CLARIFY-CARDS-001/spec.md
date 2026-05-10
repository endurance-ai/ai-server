---
id: SPEC-CLARIFY-CARDS-001
version: 0.1.0
status: completed
created: 2026-05-07
updated: 2026-05-07
author: hchsa77@gmail.com
priority: P1
issue_number: null
---

# SPEC-CLARIFY-CARDS-001: Inline-Keyboard Clarify Cards (Telegram weak-vision UX)

## HISTORY

- 2026-05-07 (v0.1.0): 최초 초안. Roadmap A3 — `ask_clarify` 노드를 자유 텍스트
  질문에서 인라인 키보드 옵션 카드로 교체. 자매 SPEC인 SPEC-VISION-UNIFY-001
  (rich Vision 스키마 — `subcategory` / `fit` / `formality` / `mood` / `style`
  필드 도입)과 SPEC-AGENTIC-CRITIQUE-001 (`crit:*` 콜백 패턴, Reflexion 루프 +
  `boost_keywords` sticky 처리) 위에 올라간다. 채널 전송 계약은 SPEC-MSG-001을,
  그래프 토폴로지 계약은 SPEC-AGENT-001을 그대로 따른다. 본 SPEC은 weak-vision
  분기에서만 동작하며, multi-item picker(`pick_item`), 자기-비평 루프
  (`evaluator` / `apply_self_critique`), kikoai/app 웹 UI는 변경하지 않는다.

---

## Goal

`app/graphs/nodes/ask_clarify.py`는 현재 LLM이 짧은 영어 자유 텍스트 질문 한
문장을 만들어 `adapter.send_text`로 전송한다. Vision이 약한 결과를 돌려준
경우(SPEC-VISION-UNIFY-001 `_is_weak_vision_v2()` 트리거 — `subcategory` 모호,
`fit`/`colorFamily` 누락, `searchQuery` 토큰 수 미달) 사용자에게 한 번 되묻는
역할이다.

문제는 다음과 같다.

1. **응답률이 낮다.** 사용자가 텔레그램에서 자유 텍스트로 답을 입력하려면
   타이핑 마찰이 크다. 카드 한 번 탭으로 끝낼 수 있는 질문에 문장을 받는다.
2. **자유 텍스트 답은 다시 LLM 라운드를 요구한다.** 답이 들어와도 그것을
   해석하려면 `router_text` → `critique_apply` 경로의 LLM 비용을 한 번 더
   지불해야 한다. weak-vision 시점에서 가장 비용 효율이 떨어지는 부분이다.
3. **품질이 흔들린다.** LLM이 매번 다른 문장을 만들고, 그 문장이 묻는 축
   (category vs formality vs fit vs occasion)도 일정하지 않다. Vision이
   부족했던 차원과 무관한 질문이 나오기도 한다.

본 SPEC은 위 세 문제를 인라인 키보드 카드로 해결한다.

- `ask_clarify` 노드는 LLM 자유 텍스트 대신 **결정론적인 옵션 카드 한 개**를
  보낸다. 짧은 한국어 프롬프트(`≤ 60자`) 위에 3–5개 버튼을 둔다.
- 어떤 축(axis)을 물을지는 `pick_clarify_axis(vision_result)` 순수 함수가
  Vision 결과의 빈 칸을 보고 결정론적으로 고른다. 한 턴에 정확히 하나의 축만
  묻는다.
- 사용자가 버튼을 탭하면 `clarify:<axis>:<value>` 형태의 콜백이 다음
  webhook으로 들어오고, `_route_after_ingest`가 새 `apply_clarify` 노드로
  분기시켜 구조화된 보강을 적용한 뒤 곧장 `search_node`로 진입한다.
- 사용자가 버튼 대신 자유 텍스트로 답해도 동작한다. 이 경로는 기존
  `router_text` → `critique_apply` 폴백을 그대로 사용해 우아하게 퇴화시킨다.

본 SPEC은 **WHAT을 정의한다**. 정확한 한국어 라벨 사본, 축별 버튼 매핑 표의
완전한 enum 값, 콜백 라우팅 와이어업 코드, 단위/E2E 테스트 시나리오는
`plan.md`/`acceptance.md`에서 다룬다.

## Non-Goals

- **kikoai/app 웹 UI 변경.** 본 SPEC은 텔레그램 채널 한정이다. 웹은 별도의
  Vision 신뢰도 흐름과 그 자체의 clarify UX(폼 기반)를 가지며 본 SPEC이
  건드리지 않는다.
- **다회차 clarify 체인.** 한 카드 + 한 탭 + 검색 진입이 종료 조건이다. 두
  번째 carification은 동일 세션에서 발생하지 않는다(R1, Q1 참조).
- **`pick_item` 노드 변경.** multi-item picker는 SPEC-AGENT-001 REQ-AGENT-010
  계약 그대로 유지된다. clarify는 single-item weak-vision 분기 전용이다.
- **개인화된 버튼 순서.** B4 episodic memory가 도입되기 전까지는 모든
  사용자에게 동일한 정적 우선순위 순서를 적용한다.
- **자기-비평 루프(`SPEC-AGENTIC-CRITIQUE-001`) 수정.** `apply_self_critique`
  노드와 `evaluator` 게이트는 그대로 둔다. clarify는 `evaluator` 진입 이전에
  검색 입력을 보강할 뿐이다.
- **새 Vision 호출.** 본 SPEC은 추가 Vision LLM 라운드를 일으키지 않는다.
  기존 Vision 결과의 빈 칸을 보고 카드를 선택할 뿐이다.
- **Telegram이 아닌 채널.** 슬랙, 카카오톡, 이메일 등은 SPEC-MSG-001
  `MessengerAdapter`가 인라인 키보드 동등 기능을 제공하지 않으므로 텔레그램
  어댑터에 한정한다.
- **i18n.** v1은 한국어 라벨만. 영어/일본어 라벨은 Future Scope로 미룬다.

## Stakeholders

| 역할 | 이름 / 그룹 | 관심사 |
|------|------------|-------|
| Product owner | hchsa77@ | weak-vision 응답률, 검색 품질 회귀 부재 |
| Engineering owner | kikoai/ai 메인테이너 | 그래프 토폴로지 단순성 유지, 테스트 커버리지 |
| Downstream consumer | `app/pipeline/runner.py` | `boost_keywords` / `subcategory` / `searchQueryKo` 입력 |
| End user | 텔레그램 봇 사용자 | 더 적은 타이핑, 더 빠른 결과 |
| Adjacent | kikoai/app 웹 메인테이너 | 본 SPEC이 웹을 건드리지 않는다는 보장 |

## Architecture Snapshot

현재(SPEC-AGENT-001 + SPEC-VISION-UNIFY-001 이후, 본 SPEC 적용 전):

```
ingest
  └─(photo)→ resolve_image → vision_node
                                  └─(weak-vision v2)→ ask_clarify  ← 자유 텍스트 LLM 한 문장
                                                          └→ END (사용자가 자유 텍스트로 답하길 기다림)
                                  └─(single+clear)→ critique_apply → search_node → ...
                                  └─(multi)→ pick_item → END
```

본 SPEC 적용 후:

```
ingest
  └─(callback `clarify:*`)─────────→ apply_clarify → search_node → ... (NEW edge)
  └─(callback `crit:*`)────────────→ critique_apply              (unchanged)
  └─(callback `item:*`)────────────→ pick_item                    (unchanged)
  └─(photo)→ resolve_image → vision_node
                                  └─(weak-vision v2)→ ask_clarify  ← 인라인 키보드 카드 1개
                                                          └→ END (탭 또는 자유 텍스트 대기)
                                  └─(single+clear)→ critique_apply → search_node → ...
                                  └─(multi)→ pick_item → END

(자유 텍스트 폴백 경로)
ingest → router_text → critique_apply → search_node → ...   (unchanged — 카드를 무시하고 텍스트로 답한 사용자)
```

핵심 변화점:

1. `ask_clarify` 노드는 더 이상 LLM을 호출하지 않는다(`_FALLBACK` 경로의
   하드코딩된 영어 한 줄도 제거). 대신 결정론적 키보드 페이로드를 만들어
   `adapter.send_text_with_buttons`로 보낸다. LLM 비용 0.
2. 새 모듈 `app/channels/clarify.py`(critique.py와 평행)가 다음을 담당한다.
   - `ClarifyAxis` enum
   - `ClarifyDelta` dataclass / Pydantic 모델
   - `parse_callback(callback_data, vision_result) -> ClarifyDelta | None`
3. 새 모듈 `app/channels/clarify_values.py`가 축별 enum 값과
   `keywords` / `subcategory_override` / `searchQueryKo_augment` 매핑 표를
   가진다.
4. 새 노드 `app/graphs/nodes/apply_clarify.py`가 `ClarifyDelta`를
   `WorkingState`에 풀어 넣고 `search_node`로 진입할 검색 입력을 보강한다.
5. `_route_after_ingest`에 `cb.startswith("clarify:")` 분기 1개 추가.
6. `SessionState.AWAITING_CLARIFY` 추가, `WorkingState`에 `clarify_axis` /
   `clarify_value` 필드 추가.

## Schema Reference

### Vision 결과(읽기 전용 입력)

본 SPEC은 SPEC-VISION-UNIFY-001이 정의한 rich VisionItem을 그대로 소비한다.
축 선택에 사용하는 필드:

- `subcategory: str` — 의류 하위 카테고리. 비어 있거나 `ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES` 안에 있으면 `subcategory_disambiguation` 후보.
- `fit: str` — 핏. 비어 있거나 enum 밖이면 `fit` 후보.
- `formality: str` — 격식도. 비어 있거나 `"ambiguous"`면 `formality` 후보.
- `mood.occasion: list[str]` — TPO 태그. 비어 있거나 generic이면 `occasion` 후보.
- `style.aesthetic: str` — 미학 노드(생성형 활용 가능, 본 SPEC에서는 옵션값
  매핑에만 참고).
- `style.detectedGender: str` — 성별. 본 SPEC은 직접 묻지 않으나
  `searchQueryKo_augment`에서 보존한다.

### 신규 모델 (`app/channels/clarify.py`)

```
ClarifyAxis: StrEnum
    CATEGORY_PICK              # 의류 대분류
    FORMALITY                  # 캐주얼 / 세미포멀 / 포멀 / 스트릿
    FIT                        # 오버사이즈 / 레귤러 / 슬림 / 크롭
    OCCASION                   # 데일리 / 오피스 / 데이트 / 운동
    SUBCATEGORY_DISAMBIGUATION # 알려진 모호 subcategory 분리
    GENERIC_FALLBACK           # 코트 / 셔츠 / 팬츠 / 신발 + 건너뛰기

ClarifyDelta:
    axis: ClarifyAxis
    value: str                          # enum 값 (예: "semi_formal")
    keywords_to_boost: list[str]        # boost_keywords 로 흐름
    subcategory_override: str | None    # search 요청 시 강제 적용
    searchQueryKo_augment: str | None   # 한국어 검색어에 덧붙임
    raw_callback: str                   # 디버그/관측용 원본 (예: "clarify:formality:semi_formal")
```

### 콜백 페이로드 형식

```
clarify:<axis>:<value>
    axis  ∈ ClarifyAxis (snake_case 값)
    value ∈ 해당 axis의 enum 값 (snake_case)

예:
    clarify:formality:semi_formal
    clarify:fit:oversize
    clarify:occasion:office
    clarify:category_pick:top
    clarify:generic_fallback:skip   ← 사용자가 "건너뛰기" 선택
```

Telegram inline button `callback_data`는 64바이트 한도가 있다. 위 형식은 모든
축/값 조합에서 안전하게 그 안에 들어간다(R3 mitigation).

## Requirements (EARS)

### REQ-CLARIFY-CARD-001 — 인라인 키보드 렌더링 의무

**WHEN** `ask_clarify` 노드가 호출되었고 `pick_clarify_axis(vision_result)`가
non-`None` 축을 반환했을 때, **THE** 시스템 **SHALL** Telegram inline keyboard
1개를 사용자에게 전송한다(자유 텍스트 질문 SHALL NOT).

키보드 요건:

- 키보드 위에 표시되는 본문 텍스트는 **60자 이하**의 한국어 프롬프트.
- 버튼 개수는 **3개 이상 5개 이하**(skip 버튼 포함).
- 어댑터 호출은 `adapter.send_text_with_buttons(chat_id, body, buttons)`
  (이미 `pick_item` 노드가 사용하는 인터페이스).

### REQ-CLARIFY-CARD-002 — Skip(건너뛰기) 버튼 의무

**THE** clarify 카드 **SHALL** 마지막 버튼으로 "건너뛰기" 옵션을 포함한다.
탭 시 콜백 페이로드는 `clarify:<axis>:skip`이며, `apply_clarify` 노드는
delta 없이 `search_node`로 그대로 진입한다(검색 보강 없음, weak-vision 그대로
검색).

### REQ-CLARIFY-CARD-003 — 버튼 라벨 길이 가드

**THE** 시스템 **SHALL** 모든 버튼 라벨이 가독성 한계 내(권장 16자 이하,
하드 한도 64바이트)에 머무는지 단위 테스트로 검증한다(R3, R4 mitigation).
이모지는 라벨에 포함하지 않는다.

### REQ-CLARIFY-AXIS-SELECTION-001 — 정확히 한 축

**WHEN** Vision 결과가 weak로 판정되었을 때, `pick_clarify_axis(vision_result)`
**SHALL** 정확히 하나의 `ClarifyAxis` 또는 `None`을 반환한다. 함수는 순수
(side-effect 없음, 외부 호출 없음, 결정론적)이며 `WorkingState`나 어댑터를
참조하지 않는다.

### REQ-CLARIFY-AXIS-SELECTION-002 — 우선순위 결정론

**THE** `pick_clarify_axis` 함수 **SHALL** 다음 우선순위 순서로 빈 칸을 검사해
첫 매치를 반환한다.

1. `category_pick` — Vision이 단일 아이템을 잡았으나 대분류 자체가 불확실할 때
   (`subcategory`가 비고 `style.detectedGender`도 비어 카테고리 추측이 약함).
2. `formality` — `formality`가 비거나 `"ambiguous"`.
3. `fit` — `fit`이 비거나 enum 밖.
4. `occasion` — `mood.occasion`이 비거나 generic-만 포함(`["daily"]` 단독 등).
5. `subcategory_disambiguation` — `subcategory`가 알려진 모호 후보
   (`settings.ask_clarify_ambiguous_subcategories`).
6. `generic_fallback` — 위 조건 모두 불충분하지만 weak-vision 자체는 참인
   경우(잡식성 안전망).

두 축이 동시에 weak일 때, 위 순서가 결정한다(Q6: priority list order).

### REQ-CLARIFY-CALLBACK-001 — 콜백 스키마 + 파서

**THE** 시스템 **SHALL** `app/channels/clarify.py`에 `parse_callback(callback_data, vision_result) -> ClarifyDelta | None` 함수를 제공한다(critique.py의
`parse_callback`과 평행 구조). 입력 형식이 `clarify:<axis>:<value>`이고 axis와
value가 둘 다 알려진 enum이면 `ClarifyDelta`를 반환하고, 그 밖의 경우(잘못된
형식, 알 수 없는 축, 알 수 없는 값) `None`을 반환한다.

### REQ-CLARIFY-CALLBACK-002 — 그래프 라우팅 와이어업

**WHEN** `_route_after_ingest`가 호출되었고 `msg.callback_data`가
`"clarify:"`로 시작할 때, **THE** 라우터 **SHALL** `apply_clarify` 노드로
분기한다. `apply_clarify` 노드의 다음 엣지는 `search_node`이다(unconditional).

### REQ-CLARIFY-CALLBACK-003 — 자유 텍스트 폴백 보존

**WHILE** 사용자가 clarify 카드를 무시하고 자유 텍스트로 답한 경우(콜백
페이로드가 아닌 일반 텍스트 메시지가 들어옴), **THE** 시스템 **SHALL** 기존
`router_text` → `critique_apply` 경로를 그대로 사용해 graceful degradation을
유지한다. 본 경로는 본 SPEC에서 신규 LLM 비용을 일으키지 않는다(이미 SPEC-
AGENT-001 / SPEC-AGENTIC-CRITIQUE-001 비용 안에 있음).

### REQ-CLARIFY-VALUE-MAPPING-001 — 구조화된 값 → 검색 입력 매핑

**THE** 시스템 **SHALL** `app/channels/clarify_values.py`에 각 (axis, value)
쌍에 대해 다음 셋 중 0개 이상을 정의한 매핑 표를 둔다.

- `keywords_to_boost: list[str]` — `boost_keywords`로 흐른다(`exclude_keywords`
  로 흐르지 않는다 — R6 mitigation: self-critique fast-path가
  `keywords`를 떨어뜨려도 `boost_keywords`는 sticky하다).
- `subcategory_override: str | None` — pipeline의 `RecommendRequest.subcategory`
  로 강제 적용.
- `searchQueryKo_augment: str | None` — `searchQueryKo`에 공백 결합.

매핑 표는 코드(파이썬 dict)로 관리되며 모든 enum 값이 표 안에 존재해야 한다는
부분은 단위 테스트가 검증한다.

### REQ-CLARIFY-STATE-001 — 세션 FSM 확장

**THE** `app/channels/session.py` `SessionState` enum **SHALL** 신규 멤버
`AWAITING_CLARIFY = "awaiting_clarify"`를 추가한다.

전이 규칙:

- `ask_clarify` 노드가 카드 전송에 성공한 후: `SessionState.AWAITING_CLARIFY`로
  전이.
- `apply_clarify` 노드 진입 시: 기존 동작(검색 진입 → `RESULTS_SENT` 또는
  `respond`)으로 전이. clarify 상태에 머무르지 않는다.
- 사용자가 `AWAITING_CLARIFY` 상태에서 자유 텍스트로 답한 경우: 기존 텍스트
  분기 규칙(`AWAITING_INTENT` 또는 `RESULTS_SENT` 분기 정의)에 따른다. clarify
  상태는 다음 webhook에서 자연 만료된다(별도 타임아웃 추가하지 않음).

### REQ-CLARIFY-STATE-002 — WorkingState 확장

**THE** `WorkingState` 모델 **SHALL** 다음 필드를 추가한다.

```
clarify_axis: ClarifyAxis | None = None
clarify_value: str | None = None
clarify_delta: ClarifyDelta | None = None  # apply_clarify 노드가 채움
```

세 필드 모두 last-writer-wins(reducer 없음). 본 필드는 `Session`에 영구
저장하지 않는다(턴 단위 스크래치패드 — REQ-STATE-005 일관).

### REQ-CLARIFY-COMPAT-001 — 자유 텍스트 폴백(`pick_clarify_axis` None 경로)

**IF** `pick_clarify_axis(vision_result)`가 `None`을 반환했다면, **THEN** `ask_clarify`
노드 **SHALL** 기존 자유 텍스트 클래리파이 동작(LLM 한 라운드 + 자유 텍스트
전송)을 그대로 수행한다(완전 후방 호환). 이 경로는 본 SPEC 적용 후에도 일부
극단 케이스(스키마 v1 잔존, vision_result `None`)에서 유효하다.

### REQ-CLARIFY-COMPAT-002 — 기능 플래그

**THE** 시스템 **SHALL** `CLARIFY_CARDS_ENABLED` 환경 변수(기본 `true`)를
지원한다. `false`로 설정하면 `ask_clarify` 노드가 카드를 보내지 않고 본 SPEC
적용 이전(SPEC-VISION-UNIFY-001 당시)의 자유 텍스트 동작으로 100% 회귀한다.
운영 중 카드 UX가 회귀를 일으킬 경우의 비상 스위치이다.

### REQ-CLARIFY-COMPAT-003 — 기존 테스트 보존

**THE** 본 SPEC 변경 사항 **SHALL** 기존 189개 테스트(plan.md/acceptance.md
시점 기준 측정)를 모두 통과시킨다. 추가되는 테스트는 본 SPEC 신규 분기에
대한 것만 허용한다.

### REQ-CLARIFY-OBSV-001 — 관측

**THE** `ask_clarify` 노드 **SHALL** Langfuse `@observe` span에 다음
속성을 기록한다.

- `axis_chosen`: `ClarifyAxis` 값 또는 `null`
- `axis_candidates_considered`: `pick_clarify_axis`가 검사한 축 목록(우선순위
  순서)
- `button_count`: 전송한 버튼 수(skip 포함)
- `user_action`: 다음 turn에서 콜백 / 자유 텍스트 / 무응답(이번 턴에서는 미정)

추가로 표준 로거에 다음 라인을 남긴다.

```
[CLARIFY] axis=formality buttons=4 ko_prompt="이 옷, 어디서 입을 거예요?"
```

`apply_clarify` 노드도 동일 패턴으로 적용 결과를 한 줄 로깅한다(`[CLARIFY-APPLY]
axis=formality value=semi_formal subcategory_override=null boost_keywords=...`).

### REQ-CLARIFY-OUT-OF-SCOPE-001 — 명시적 비범위

**THE** 본 SPEC **SHALL NOT** 다음을 변경한다.

- `app/api/recommend.py` 또는 `kikoai/app`의 어떤 라우트 / 컴포넌트
- `pick_item` 노드(SPEC-AGENT-001 REQ-AGENT-010)
- `evaluator` / `apply_self_critique` 노드(SPEC-AGENTIC-CRITIQUE-001)
- `app/channels/vision.py`의 Vision 호출 또는 프롬프트
- 다중 턴 clarify 체인 / 개인화된 버튼 순서

위 요소를 손대야 하는 경우 본 SPEC을 닫고 별도 SPEC을 연다.

## Env Vars

| 키 | 기본값 | 효과 |
|----|--------|------|
| `CLARIFY_CARDS_ENABLED` | `true` | `false`일 때 `ask_clarify`가 본 SPEC 이전 동작(자유 텍스트 LLM)으로 회귀. 비상 스위치(REQ-CLARIFY-COMPAT-002). |
| `CLARIFY_MAX_BUTTONS` | `5` | 카드당 버튼 상한(skip 포함). 범위 [3, 8]. |

기존 변수 재사용:

- `ASK_CLARIFY_MIN_QUERY_TOKENS`, `ASK_CLARIFY_MIN_DESC_TOKENS`,
  `ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES`, `ASK_CLARIFY_AMBIGUOUS_LABELS` —
  weak-vision 트리거 정의(routing.py에 이미 존재).

신규 LLM 호출이 없으므로 `LITELLM_*` / `RESPONSE_MODEL` / `RESPONSE_TIMEOUT_MS`
변수는 본 SPEC 경로에서 사용하지 않는다(`CLARIFY_CARDS_ENABLED=false` 폴백
경로에서만 기존대로 사용).

## Risks

- **R1 — 버튼 과부하 / 사용자 피로.** clarify 카드를 매 weak-vision마다 띄우면
  대화가 답답해진다.
  - Mitigation: REQ-CLARIFY-AXIS-SELECTION-001(한 턴에 한 축). Q1 lean(세션당
    1회 캡)을 acceptance.md에서 검증하되 본 SPEC v1은 기본적으로 weak-vision
    분기에 한정되어 있어 자연 빈도가 낮다.
- **R2 — Vision 스키마 드리프트로 axis 선택 깨짐.** SPEC-VISION-UNIFY-001
  스키마가 바뀌면 `pick_clarify_axis`가 잘못된 분기를 탈 수 있다.
  - Mitigation: snapshot 단위 테스트를 `vision_prompt.py` 상수와 동기화. 빈
    `subcategory` / 잘못된 enum 값에 대해서는 `generic_fallback`으로 안전하게
    퇴화.
- **R3 — Telegram inline button 50자(라벨) / 64바이트(callback_data) 한도.**
  한국어 라벨 + skip 버튼이 한도를 넘을 수 있다.
  - Mitigation: REQ-CLARIFY-CARD-003 단위 테스트가 모든 라벨 + 모든 callback_data
    조합을 정적으로 검증.
- **R4 — iOS / Android 한국어 렌더 차이.** 일부 한자/이모지가 플랫폼에 따라
  깨진다.
  - Mitigation: 라벨에 이모지/특수문자 사용 금지(REQ-CLARIFY-CARD-003). 일반
    한글 + 영문 조합만 허용.
- **R5 — 사용자가 탭 후 즉시 자유 텍스트 입력(레이스).** 콜백과 텍스트가 빠르게
  연달아 도착할 수 있다.
  - Mitigation: 텔레그램 webhook은 메시지 단위 직렬화이고, 본 SPEC은 콜백 우선
    경로(`apply_clarify`)와 텍스트 우선 경로(`router_text`)를 둘 다 가지므로
    어느 쪽이 먼저 들어와도 graceful. 두 webhook이 서로 다른 흐름을 끝까지
    독립 실행한다.
- **R6 — clarify keywords가 self-critique fast-path에서 떨어짐.** SPEC-AGENTIC-
  CRITIQUE-001의 자기-비평 루프가 `keywords`를 흔들어 사용자가 답한 차원이
  사라질 수 있다.
  - Mitigation: REQ-CLARIFY-VALUE-MAPPING-001은 `boost_keywords`(sticky)에만
    값을 넣는다. 이름 그대로 `boost`이므로 자기-비평 루프가 보존한다.

## Exclusions (What NOT to Build)

본 SPEC이 의도적으로 만들지 **않는** 것들. 이 목록은 개발자가 "마침 이 자리에
와 있는 김에" 추가하는 드라이브-바이 변경을 차단한다.

1. **kikoai/app 웹 UI 변경 일체.** `src/components/`, `app/(routes)/`,
   `src/lib/analyze/` 어떤 파일도 본 SPEC 범위가 아니다.
2. **다중 턴 clarify(2개 이상 카드 연속 표시).** 한 turn에 한 카드. 두 개 축이
   동시에 weak이라도 우선순위 1개만 묻는다.
3. **`pick_item`(multi-item picker) 변경.** picker는 multi-item 분기 전용이며
   clarify와 결합하지 않는다.
4. **개인화 / 학습 기반 버튼 순서.** 모든 사용자에게 동일한 정적 순서. 개인화는
   B4 episodic memory가 도착한 뒤 별도 SPEC.
5. **자기-비평 루프 수정.** `evaluator`, `apply_self_critique`, `critique_trail`
   안 건드린다.
6. **Vision 호출 추가.** 본 SPEC은 추가 Vision 라운드를 일으키지 않는다.
   기존 결과를 보고 카드 선택만 한다.
7. **새 LLM 호출.** 카드 본문 / 버튼 라벨 / 매핑 표 모두 결정론적. `apply_clarify`
   노드도 LLM 호출 없음. (`CLARIFY_CARDS_ENABLED=false` 폴백만 LLM 사용).
8. **다국어(영어/일본어) 라벨.** v1은 한국어만(Q2 lean).
9. **카드에 이미지/썸네일.** 텍스트 + 버튼만(Q3 lean). 이미지 첨부는 별도
   `sendPhoto` 라운드가 필요하고 응답 지연을 키운다.
10. **A/B 실험 인프라.** 카드 vs 텍스트 비교는 dev 봇에서 1주일 관찰 후 결정
    (Q8 lean). 본 SPEC은 실험 분기를 만들지 않는다.

## Open Questions

| ID | 질문 | 현재 lean | 결정 시점 |
|----|------|----------|----------|
| Q1 | clarify 카드를 세션당 max 1회로 캡할지, 이미지당 max 1회로 캡할지? | **세션당 1회**(R1 mitigation). 사용자가 같은 세션에서 여러 사진을 보내도 두 번째 사진부터는 카드 없이 곧장 검색. | acceptance.md 작성 시 |
| Q2 | v1에서 한국어 라벨만 지원할지, 시작부터 i18n(영문/일문 동시) 갈지? | **한국어만**. 텔레그램 봇 사용자가 한국어 사용자에 집중되어 있고, i18n은 라벨 길이 / 라인브레이크 / Telegram 인코딩 변수를 추가한다. | v1 출시 후 dev 운영 1주 관찰 |
| Q3 | clarify 카드에 Vision이 잡은 아이템 썸네일을 함께 보낼지? | **텍스트 카드만** v1. 썸네일은 `sendPhoto` 추가 라운드가 필요하고 카드 응답 지연을 늘린다. | v1 출시 후 사용자 피드백 |
| Q4 | 카드 발행 후 사용자가 다음 사진을 먼저 보냈을 때(stale 콜백)? | **stale은 무시 + toast**. `apply_clarify`가 진입하더라도 현재 세션의 `vision_result`가 다른 사진의 것이면 `discard with toast`(`adapter.answer_callback_query("이미 다른 사진을 받았어요")`) 후 종료. | plan.md 단계 |
| Q5 | `apply_clarify`를 별도 노드로 만들지, `critique_apply`를 확장할지? | **별도 노드**. critique와 clarify는 의미·관측·테스트 경계가 분명히 다르고, `critique_apply`는 SPEC-AGENTIC-CRITIQUE-001 계약에 묶여 있다. | 본 SPEC 확정 |
| Q6 | 동일 우선순위에서 두 축이 같은 가중치로 weak일 때? | **우선순위 리스트 순서**(REQ-CLARIFY-AXIS-SELECTION-002의 1→6 순서). | 본 SPEC 확정 |
| Q7 | clarify가 만든 `boost_keywords`가 self-critique 루프에서 살아남아야 하는지? | **sticky**(`boost_keywords` 사용 — REQ-CLARIFY-VALUE-MAPPING-001 / R6 mitigation). | 본 SPEC 확정 |
| Q8 | 카드 vs 텍스트 A/B 분기를 v1에 넣을지? | **v1 미포함**. dev 봇에서 1주 관찰 후 결정. 운영 데이터 부족 단계에서 분기를 추가하는 비용이 효익보다 크다. | dev 운영 1주 후 |

## Future Scope

- **B4 episodic memory와 결합한 개인화 버튼 순서.** 사용자 과거 답변에 기반해
  Top-k 옵션을 우선 표시.
- **Multi-axis clarify**(예: 2행 키보드로 formality + occasion 동시). v1
  데이터를 본 후 응답률·정확도 영향이 있을 때만.
- **i18n** — 영어 / 일본어 라벨 셋. 한국어 운영 안정화 후.
- **Onboarding clarify**(B6 — 신규 사용자 환영 메시지에서 취향 카드 사용).
  본 SPEC의 `ClarifyAxis` / `ClarifyDelta` / 매핑 표 인프라를 그대로 재사용.
- **카드 + 썸네일 패턴.** Q3가 v1 운영 후 효익 있다고 판단되면 추가.
- **A/B 실험 분기.** Q8 lean 결정 후.

## Definition of Done

본 SPEC은 다음이 모두 참일 때 종료된다(상세 acceptance.md 참조).

- [ ] `app/channels/clarify.py` 신설(`ClarifyAxis`, `ClarifyDelta`,
      `parse_callback`).
- [ ] `app/channels/clarify_values.py` 신설(축별 enum + 매핑 표). 모든 enum 값이
      매핑 표에 존재함을 단위 테스트가 검증.
- [ ] `app/graphs/nodes/ask_clarify.py` 재작성: `pick_clarify_axis` →
      카드 전송 경로(LLM 0회 호출, `CLARIFY_CARDS_ENABLED=true` 시).
- [ ] `app/graphs/nodes/apply_clarify.py` 신설: `ClarifyDelta`를
      `WorkingState`에 풀어 넣고 검색 입력 보강.
- [ ] `_route_after_ingest`에 `clarify:*` 콜백 분기 1개 추가.
- [ ] `SessionState.AWAITING_CLARIFY` 추가, `WorkingState`에 3개 필드 추가.
- [ ] 4개 이상 축(category_pick / formality / fit / occasion)이 종단 간
      동작.
- [ ] 단위 테스트 6개 이상 + 그래프-레벨 E2E 테스트 2개 이상.
- [ ] 본 SPEC 적용 전 189개 기존 테스트 전부 통과.
- [ ] `CLARIFY_CARDS_ENABLED=false` 회귀 시 본 SPEC 이전 동작과 100% 동일.
- [ ] `pick_clarify_axis` 함수에 `# @MX:ANCHOR` 태그(Vision 결과 → axis 선택은
      외부 호출이 의존하는 결정 경계 / `code_comments=ko` 시 한국어 description).
- [ ] Conventional commit 메시지 초안 작성(`feat(clarify): inline-keyboard
      clarify cards (SPEC-CLARIFY-CARDS-001)`).
- [ ] 텔레그램 dev 봇 수동 테스트 시나리오를 acceptance.md에 명시(이미지 5장:
      formality weak, fit weak, occasion weak, multi-axis weak,
      generic_fallback).
- [ ] Langfuse 트레이스에 신규 attribute 4개(`axis_chosen`,
      `axis_candidates_considered`, `button_count`, `user_action`)가 기록되는지
      dev 환경에서 검증.

---

**참조 SPEC**: SPEC-MSG-001 (채널 어댑터 / inline keyboard 계약),
SPEC-AGENT-001 (그래프 토폴로지 / 라우팅 계약), SPEC-VISION-UNIFY-001 (rich
Vision 스키마 — `subcategory` / `fit` / `formality` / `mood` / `style`),
SPEC-AGENTIC-CRITIQUE-001 (`crit:*` 콜백 패턴 / `boost_keywords` sticky 계약).

---

## Implementation Notes

**구현 완료일**: 2026-05-07
**Merge**: PR [#12](https://github.com/endurance-ai/ai-server/pull/12), commit `26faa32` on `dev`
**추가 테스트**: +74 (전체 263 / 263 통과)

### 추가된 파일

- `app/channels/clarify.py` — ClarifyAxis(StrEnum), ClarifyDelta(Pydantic v2), parse_callback, pick_clarify_axis (# @MX:ANCHOR 태그 포함)
- `app/channels/clarify_values.py` — 축별 enum 값 + keywords/subcategory_override/searchQueryKo_augment 매핑 표
- `app/graphs/nodes/apply_clarify.py` — ClarifyDelta를 WorkingState에 풀어 넣고 search_node 검색 입력 보강
- `tests/channels/test_clarify.py` — ClarifyAxis 선택 우선순위, parse_callback, 버튼 라벨 길이 가드, 매핑 표 완전성 단위 테스트
- `tests/test_graph_nodes/test_apply_clarify.py` — apply_clarify 노드 단위 테스트 (skip 분기, 각 축별 보강 검증)
- `tests/test_graph_flows.py` (확장) — clarify:* 콜백 E2E 시나리오 2개 이상 추가

### 수정된 파일

- `app/graphs/nodes/ask_clarify.py` — LLM 호출 제거, pick_clarify_axis → 결정론적 인라인 키보드 카드 전송으로 재작성; CLARIFY_CARDS_ENABLED=false 시 기존 LLM 폴백 경로 보존
- `app/graphs/routing.py` — _route_after_ingest에 clarify:* 콜백 분기 추가
- `app/graphs/state.py` — WorkingState에 clarify_axis / clarify_value / clarify_applied 필드 추가
- `app/channels/session.py` — SessionState에 AWAITING_CLARIFY 상태 추가
- `app/core/config.py` — CLARIFY_CARDS_ENABLED / ASK_CLARIFY_AMBIGUOUS_SUBCATEGORIES 선언
- `.env.example` — 위 환경변수 문서화

### 이연(Deferred) 항목

- **Langfuse 스팬 attribute 보강** — axis_chosen / axis_candidates_considered / button_count / user_action 4개 structured 로그 라인은 구현되어 있으나 Langfuse 스팬 metadata attribute로는 연결되지 않음. @observe 래퍼 확장이 필요하며 REQ-VISION-OBSV-001 이연과 같은 이유로 별도 observability SPEC에서 처리 예정.
- **텔레그램 dev 봇 수동 테스트 시나리오 문서** — acceptance.md에 5장 이미지 시나리오(formality weak, fit weak, occasion weak, multi-axis weak, generic_fallback) 명시 필요. PR #12 이후 dev 봇 운영 중 별도 문서화 예정.

### 해소된 Open Questions

- Q1 clarify cap: 세션당 1회(SessionState.AWAITING_CLARIFY로 관리)로 확정
- Q2 i18n: v1 한국어 라벨만으로 확정
- Q3 썸네일: 텍스트 카드만으로 확정
- Q5 apply_clarify: critique_apply와 별도 노드로 확정
- Q6 우선순위 동점: 1→6 리스트 순서로 확정
- Q7 boost_keywords sticky: boost_keywords 사용으로 확정
