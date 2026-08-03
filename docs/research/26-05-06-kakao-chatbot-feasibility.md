# 카카오톡 챗봇 도입 타당성 조사

- **작성일**: 2026-05-06 (5/5 standup carry-over: "카카오톡 챗봇 만들 수 있는지 조사")
- **컨텍스트**: 텔레그램 봇(SPEC-MSG-001) 구조 그대로 카톡으로 확장 가능한지 + 등록/수익화 절차 파악
- **결론(TL;DR)**: 기술적으로 **가능**. 단 ①5초 응답 SLA → 콜백 패턴 필수, ②자체 수익화 모델 없음 → 유입 채널/CS 자동화 용도, ③비즈니스 채널 인증(영업일 3~5일) 필요

---

## 1. 구조 비교 — 텔레그램 vs 카카오톡

| 항목 | 텔레그램 (현재) | 카카오톡 |
|---|---|---|
| 진입 단위 | 봇 토큰 + webhook | **카카오톡 채널** + **카카오 i 오픈빌더** 챗봇 (한 쌍) |
| UI 정의 | 코드로 자유 | 시나리오 / 블록 / 엔티티 GUI 빌더 강제 |
| 외부 서버 | FastAPI webhook 직결 | **스킬(Skill) 서버**가 외부 webhook 역할 |
| 자유 텍스트 입력 | 자유 | "폴백 블록"으로만 받음 (정의 안 된 발화) |
| 응답 시간 | 무제한 | **5초 SLA** / 초과 시 **콜백 패턴** (콜백 URL 1분 유효, 1회) |
| 응답 포맷 | 마크다운/이미지 자유 | 정해진 JSON: `simpleText` / `basicCard` / `listCard` / `carousel` |

**핵심 의미**: 카톡은 우리 봇 인스턴스를 그대로 띄우는 게 아니라, **카카오 인프라(채널 + 오픈빌더)에 우리 스킬 서버를 webhook으로 꽂는** 형태. → kikoai/ai에 `/skills/kakao/*` 엔드포인트만 추가하면 검색·LLM·DB 핵심 로직은 그대로 재사용.

## 2. 가장 큰 기술 게이트: 5초 응답 타임아웃

우리 흐름 `enhance_query → embedding → vector search → LLM` 은 5초 안에 끝나기 어려움.

**해결책 — 카카오 공식 콜백(useCallback) 패턴**:
1. 첫 응답 (≤5초): "검색 중이에요…" + `useCallback: true` 반환
2. 백그라운드에서 검색·LLM 처리
3. 결과 나오면 카카오가 발급한 `callback_url`로 1분 안에 POST → 사용자에게 결과 카드 노출

→ kikoai/ai에 **비동기 작업 큐 + 콜백 POST** 추가 구현 필요. LangGraph 워크플로우는 그대로 재사용.

## 3. 등록 절차 (사업자 등록 가정)

1. 카카오톡 채널 관리자센터에서 **채널 개설** (무료)
2. **비즈니스 채널 인증** 신청
   - 개인사업자: 카카오톡 전자증명서
   - 법인: 사업자등록증 + 휴대폰 본인인증
3. **심사 영업일 3~5일**
4. 카카오 i 오픈빌더 가입 → 봇 생성 → 채널 연결
5. 시나리오/블록 정의 + 스킬 등록 (kikoai/ai webhook URL)
6. 검수 신청 → 통과 시 일반 사용자에게 노출

> 데모/테스트는 **검수 전에도 본인을 채널 친구로 추가해서 가능**. 정식 배포만 검수 필요.

## 4. 비용 구조 (2026-05 기준)

| 항목 | 비용 |
|---|---|
| 채널 개설 / 비즈니스 인증 | 무료 |
| 카카오 i 오픈빌더 챗봇 (대화/응답) | **무료** (2022-09부터 무료 전환) |
| Event API 푸시 메시지 | 건당 15원 |
| 친구톡(마케팅) | 텍스트 15원 / 이미지 20원 / 와이드 23원 (VAT 별도) |
| 알림톡(정보성) | 친구톡과 비슷, 광고 문구 불가 |

→ 사용자가 먼저 말 걸어서 답하는 한 비용 0. **발송형 마케팅**할 때만 돈 나감.

## 5. 수익화 가능성

카톡 자체에는 유튜브식 광고 수익 모델 **없음**. 가능한 패턴:

1. **유입 채널** — 카톡에서 검색 → 결과 카드의 외부 링크 → 우리 웹앱 결제/제휴 (가장 일반적, 우리 적합)
2. **친구톡 마케팅** — 친구 추가한 사용자에게 큐레이션 발송. 단가 vs 전환율 계산 필요
3. **B2B SaaS** — 패션 브랜드에 카톡 챗봇 납품 (다른 사업)

**판단**: 카카오톡 챗봇 = 직접 매출 X, **유입·리텐션 도구**.

## 6. 우리 코드베이스 적용 시 추가 작업

- `app/channels/kakao/` 신규 — 텔레그램 채널 패턴(SPEC-MSG-001 / Port + Protocol)을 그대로 따라
- 카카오 SkillResponse 스키마 어댑터 (`SearchProduct` shape → `listCard` / `carousel`)
- 콜백 패턴 구현 — 즉시 응답 + 백그라운드 작업 + 콜백 POST
- 카카오톡 응답 길이 제한 / 이모지 / 버튼 액션(외부 링크·딥링크) 매핑 테이블

기존 `KakaoChannel`을 채널 모듈에 추가하는 형태로 그림이 맞음.

## 7. 권장 다음 액션 (5/8 런칭 이후 검토)

1. 사업자등록 완료 → 채널 개설 + 비즈니스 인증 (영업일 3~5일)
2. **콜백 패턴 PoC** — 5초 응답 + 비동기 결과 전송이 LangGraph 흐름에서 깨지지 않는지 검증 (가장 큰 리스크)
3. listCard / carousel 스키마에 결과 카드(브랜드·가격·이미지·링크)가 들어가는지 매핑 테스트
4. 정식 검수 신청

---

## 참조

- [카카오 i 오픈빌더 공식](https://i.kakao.com/)
- [카카오 i 오픈빌더 챗봇 요금 안내](https://i.kakao.com/pricing)
- [카카오톡 채널 챗봇 무료 전환 공지 (kakaocorp)](https://www.kakaocorp.com/page/detail/9756)
- [챗봇은 무료인가요? — 카카오 고객센터](https://cs.kakao.com/helps_html/1073202191?locale=ko)
- [비즈니스 채널 신청 가이드 — 카카오 고객센터](https://cs.kakao.com/helps_html/1073204966?locale=ko)
- [카카오톡 비즈니스 채널 만들기 (kakao business)](https://kakaobusiness.gitbook.io/main/channel/start)
- [AI 챗봇 콜백 개발 가이드 (kakao business)](https://kakaobusiness.gitbook.io/main/tool/chatbot/skill_guide/ai_chatbot_callback_guide)
- [스킬 만들기 (kakao business)](https://kakaobusiness.gitbook.io/main/tool/chatbot/skill_guide/make_skill)
- [메시지 발송 비용 — 카카오 고객센터](https://cs.kakao.com/helps_html/1073188059?locale=ko)
