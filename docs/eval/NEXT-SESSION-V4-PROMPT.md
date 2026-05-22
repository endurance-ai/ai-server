# 다음 세션 V4 시작 프롬프트

> 이 파일을 다음 세션에 그대로 붙여넣으면 됨. (또는 핵심만 복사)

---

## 붙여넣을 프롬프트

```
kiko.ai AI 서버 V4 작업 이어서 한다. 지난 세션에서 V3 ReAct 플로우를 자동평가 +
실 텔레그램 검증하고 P0/P1 + 성별핀(SPEC-GENDER-PIN-001) + refine 쿼리 재사용을
dev 에 머지했다 (PR #32). 이번엔 V4-A "검색 품질 distance 신호"를 한다.

먼저 이 3개 문서를 읽고 시작해:
- docs/eval/260522-v4-proposal.md  (V4 후보 A~F + 우선순위, §2-A 가 이번 타겟)
- docs/eval/260521-v3-eval-report.md §8~10 (실테스트로 잡은 것들)
- 메모리 project-search-distance-calibration (distance 실측 — 절대 임계값 금지)

## V4-A 목표
검색이 "결과 15개 나왔으니 성공"으로 착각하는 문제 해결. v6 는 cross-modal
(텍스트→이미지 SigLIP) 이라 distance 가 0.87~0.96 에 압축돼 있고 (완벽매칭도
~0.88, 무의미 ~0.90), min 단독은 거의 비차별적이다. → "약한 매칭을 약하다고
인식"하는 신호를 도입한다.

## 반드시 선행 (코드 박기 전)
1. distance 분포 캘리브레이션: good 쿼리(흔한 카테고리) vs bad 쿼리(니치/무의미)
   30~50개의 min/median/spread 분포를 dev-app 실데이터로 수집. 지난 세션 5개
   샘플이 시작점 (메모리 참조). 이걸로 "weak" 판정식을 도출 — 절대 임계값(0.3,
   0.55 등 RAG 수치)은 cross-modal 에 부적용이니 쓰지 말 것.
2. 판정식 후보: median > X AND spread < Y (촘촘하지만 멀다=카탈로그에 없음) /
   또는 쿼리 baseline 대비 상대값. 실측으로 결정.

## 구현 방향 (제안서 §2-A)
- search_products result_summary 에 distance 통계(min/median/spread/degraded_count)
  노출 (RPC 가 이미 반환).
- weak 판정 시 → suggest_next_step + 톤다운 멘트 ("딱 맞는 건 없는데 비슷한 걸로").
- 임계값은 env 화.
- 연계: Reflexion(Gap2) 트리거를 count==0 → weak-distribution 으로 재정의할지
  결정 (제안서 §2-B — A 하면 살리고, 안 하면 제거).

## 작업 규칙 (지난 세션과 동일)
- 로컬 서버는 `kiko-up` 으로 띄움 (DEV 봇, :8001). dev-app PG / Modal / Langfuse
  실연결. agent LLM = claude-haiku-4-5.
- 자동 검증 러너: scripts/eval/run.py (webhook 합성 + PG/Redis 관측). 단 mock
  chat_id 는 카드 실도착/cursor 측정 못 함 (V4-F 로 picker 시뮬 추가 가능).
- 커밋만 요청 시 변경 파일만 명시 add (git add -A 금지). 머지는 /feature-finalize.
- ruff check + ruff format --check 둘 다 통과해야 CI 통과 (지난번 format 빠뜨려
  CI 한 번 깨짐).
- 의심나면 RPC/코드 직접 까서 실측 (지난번 distance 0.9 오해처럼 추측 금지).
```

---

## 빠른 컨텍스트 (다음 세션 나에게)

**지난 세션 결과물 (PR #32, dev 머지)**:
- P0: axis whitelist Literal 검증, search-first policy
- P1: CDN fastpath, pending_question clear, evaluator_run/taste_update emit, pipeline_failed 상세화
- 카드 묶음(WEBPAGE_CURL_FAILED drop-retry), 콘텐츠 dedup, 타이밍 로그
- SPEC-GENDER-PIN-001 (migration 0008, 성별 카드/영구저장/override)
- refine 쿼리 재사용 (last_query.py)

**V4 남은 후보** (우선순위, 제안서 §3):
- 🥇 V4-A 검색 품질 distance 신호 ← **이번 타겟**
- 🥈 V4-B Reflexion 존폐 (A 에 묶임)
- 🥉 V4-C 모호성-인지 라우팅 (search-first 됨, distance→clarify 잔여)
- V4-D picker auto-pick (이미지 3-hop→2-hop)
- V4-E card:like → taste_update emit (quick-win)
- V4-F eval 러너 picker 시뮬 + 실chat 모드

**핵심 교훈**:
- distance 는 cross-modal modality gap 으로 0.87~0.96 압축. 절대 임계값 금지.
- Reflexion(Gap2)은 count==0 트리거라 프로덕션에서 안 터지는 죽은 코드 — A 와 함께 결정.
- 글로벌 dict(last_query/pending_gender)는 멀티워커 미지원 — 스케일 시 Redis.
- 배포: dev 머지됨, dev-ai PROD 배포는 별도 (/ai-provision 또는 수동).
