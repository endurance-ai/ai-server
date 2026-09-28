---
name: kiko-eval
description: KIKO 에이전트·검색 평가를 재현하거나 재측정할 때 사용. 라우팅(routing_eval), 가드레일(guardrail_eval), 검색 품질(search_quality_eval), 골든셋(run_goldenset) 실행법과 결과 기록 규칙. "재측정", "평가 돌려", "정확도 다시 재", "수치 확인" 요청 시.
---

# KIKO 평가 재현 런북

규칙은 `.claude/rules/kiko/evidence-and-eval.md` 2절을 따른다. 이 문서는 실행 절차다.

## 평가 목록

| 평가 | 파일 | 무엇을 재나 | 채점 |
|---|---|---|---|
| 라우팅 | `tests/eval/routing_eval.py` + `routing_dataset.json`(44케이스) | 첫 툴 선택(isolated), 실제 턴 성공(e2e) | 규칙(기대 툴 비교) |
| 가드레일 | `tests/eval/guardrail_eval.py` + `guardrail_dataset.json`(30케이스·7카테고리) | 페르소나 프롬프트 응답의 환각·유저 평가·범위 이탈 | LLM-as-judge |
| 검색 품질 | `tests/eval/search_quality_eval.py` + `search_quality_dataset.json` | 실제 retrieval 경로 top-K의 속성 적중 | 규칙(속성 라벨) |
| 골든셋 | `scripts/goldenset/run_goldenset.py` | 문형별 운영 검색 결과 | 결과 저장 후 사람 판정 |
| 멀티턴 | `tests/eval/multiturn_eval.py` + `multiturn_dataset.json` | 첫 쿼리와 후속 수정어를 섞은 벡터 검색이 원래 옷 정체성(identity)과 수정 의도(modifier)를 함께 지키는지 | 규칙(기대 속성 일치율, 조화평균) |

범위 주의: 가드레일은 페르소나 시스템 프롬프트 + 사용자 한 줄만 본다. ReAct 루프와 툴 호출은 포함하지 않는다. 문서에 인용할 때 이 범위를 같이 적는다.

## 실행 위치

dev-ai 컨테이너(`ai-server`)에서 돌린다. 컨테이너에 LiteLLM·DB·Modal env가 이미 있고, 운영과 같은 코드·페르소나로 잰다. 컨테이너에는 `tests/`와 `.git`이 없으므로 파일을 복사하고 커밋 SHA는 인자로 넘긴다.

```bash
# 로컬 → 서버 → 컨테이너
scp tests/eval/routing_eval.py tests/eval/routing_dataset.json ec2-user@<dev-ai>:/tmp/
ssh ec2-user@<dev-ai> 'docker cp /tmp/routing_eval.py ai-server:/tmp/ && docker cp /tmp/routing_dataset.json ai-server:/tmp/'
```

측정 대상 커밋은 `git log -1 --format=%h origin/dev`로 확인하고, dev 자동 배포가 그 커밋까지 끝났는지(`deploy-dev.yml` run) 먼저 본다.

### 라우팅

```bash
docker exec -w /app ai-server env PYTHONPATH=/app python /tmp/routing_eval.py \
  --mode isolated --runs 3 --sha <commit> --output /tmp/routing_isolated.json
docker exec -w /app ai-server env PYTHONPATH=/app python /tmp/routing_eval.py \
  --mode e2e --sha <commit> --output /tmp/routing_e2e.json
```

- isolated = 첫 툴만 비교(9/7 슬라이드 방식), e2e = 실제 턴(카드 전달·거짓 응답 포함).
- 결정론 라우터가 처리한 턴은 LLM 판단과 따로 집계된다. 문서에는 "전체"와 "LLM이 직접 판단한 턴"을 구분해 적는다.

### 가드레일

스크립트는 `parents[2]`로 프로젝트 루트를 찾으므로 `/app/tests/eval/`에 둔다.

```bash
docker exec ai-server mkdir -p /app/tests/eval
docker cp /tmp/guardrail_eval.py ai-server:/app/tests/eval/
docker cp /tmp/guardrail_dataset.json ai-server:/app/tests/eval/
docker exec -w /app ai-server python tests/eval/guardrail_eval.py \
  --bot-model claude-haiku-4-5 --judge-model claude-sonnet-4-5 \
  --output /tmp/guardrail_results_<YYYYMMDD>.json
```

- 기본 judge `gpt-4o-mini`는 LiteLLM의 OpenAI 키 쿼터 소진 이력이 있다. 봇과 다른 모델을 judge로 쓴다.
- 쓸 수 있는 모델은 LiteLLM `/v1/models`로 확인한다.

### 골든셋

```bash
docker cp scripts/goldenset ai-server:/app/scripts/goldenset   # 서버에 올린 뒤
docker exec -w /app ai-server python scripts/goldenset/run_goldenset.py <pattern_ids...> --gender women|men [--precision on|off]
```

결과는 `scripts/goldenset/out/`에 생기므로 `docker exec cat`으로 회수한다.

### 검색 품질

로컬 `uv run`으로 돌린다. `MODAL_EMBED_URL`, `MODAL_EMBED_TOKEN`, `KIKOAI_DEVAPP_DSN`(또는 `DB_DSN`)이 필요하고, `--rewrite`는 LiteLLM env도 필요하다. 결과 파일에 SHA와 시각이 자동으로 들어가고 `compare.py`로 비교한다.

## 측정 후 필수 확인

1. 채점 샘플 검수: 통과·실패 각각 몇 건의 원문 응답과 판정을 열어 채점이 맞는지 본다. 전부 통과·전부 실패면 특히 의심한다.
2. 측정 도구 결함 체크: 세션 언어(ko/en), 이전 실행이 남긴 상태(노출 dedupe, 캐시), 재실행 시 결과가 바뀌는지.
3. 결과 기록: 커밋 SHA, 날짜, 봇/judge 모델, 표본 수, 반복 횟수를 PR 설명이나 문서에 옮긴다. 결과 JSON은 gitignore라 기록하지 않으면 사라진다.
4. 이전 수치와 비교: 차이가 나면 측정 방식 차이인지 성능 차이인지 먼저 가른다.
