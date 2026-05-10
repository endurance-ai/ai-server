# CI/CD

> GitHub Actions + ECR + SSH 배포. 워크플로우 SoT 는 `aws-infra/.github/workflows/portal/`, 본 repo `.github/workflows/` 에 동일 복사본.

## 트리거 매트릭스

| 트리거 | 워크플로우 | 동작 |
|-------|-----------|------|
| `dev` 브랜치로 PR open / sync | `ci.yml` | ruff + pytest + Docker build 검증 (push X) |
| `dev` 에 PR merge | `deploy-dev.yml` | verify → ECR push → SSH deploy |
| Actions 수동 (`workflow_dispatch`) | `deploy-dev.yml` | 동일 |

## ci.yml — PR 검증

```yaml
on:
  pull_request:
    branches: [dev]
    paths-ignore: ["**.md", "docs/**"]
  workflow_dispatch:

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
```

Job 1: `lint-and-test` (ARM 러너)
- `astral-sh/setup-uv@v3` (`version: "0.7.x"` 고정)
- `uv sync --frozen`
- `uv run ruff check .` + `uv run ruff format --check .`
- `uv run pytest -q`

Job 2: `docker-build` (ARM 러너, `lint-and-test` 의존)
- `docker/build-push-action@v5` `push: false`, `platforms: linux/arm64`, `cache: type=gha`

`paths-ignore` 로 docs/ MD 만 변경된 PR 은 CI 건너뜀.

## deploy-dev.yml — 빌드 + 배포

```yaml
on:
  pull_request:
    types: [closed]
    branches: [dev]
  workflow_dispatch:

concurrency:
  group: deploy-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false   # 배포는 직렬화 (취소 X)
```

3-job 파이프라인:

### Job 1: verify
- 같은 ruff + pytest (배포 직전 재검증, 분기 race 방지)
- **fork PR 가드**: `head.repo.full_name == github.repository` + `merged == true` 확인
- 수동 실행은 항상 통과

### Job 2: build-and-push
- ARM 네이티브 러너 → Docker buildx → ECR push (linux/arm64)
- 태그: `<YYYYMMDDhhmmss>-<short_sha>` (KST) + `latest` 동시 태그
- AWS 인증: `aws-actions/configure-aws-credentials@v4` + `secrets.AWS_*`

### Job 3: deploy
- `appleboy/ssh-action@v1.0.3`
- `secrets.SSH_HOST/SSH_USER/SSH_PRIVATE_KEY` 로 EC2 접속
- `/home/ec2-user/scripts/deploy.ai.sh <IMAGE_TAG>` 실행
- `environment: portal-dev` 로 보호 (필요 시 manual approval 추가 가능)

## deploy.ai.sh — EC2 측 스크립트

`aws-infra/kiko-ai-servers/portal-ai/scripts/deploy.ai.sh` (EC2 의 `/home/ec2-user/scripts/`).

흐름:
1. `.env` 로드 + `IMAGE_TAG` 인자 검증
2. `aws sts get-caller-identity` 로 ECR 로그인 (EC2 IAM Role 사용)
3. 이미지 pull + `latest` 태그 동기화
4. `docker compose up -d ai-server`
5. `/health` 헬스체크 (200 또는 503 = degraded 도 부팅 성공으로 간주, 최대 120초)
6. `docker image prune -af`

## GitHub Secrets

| Secret | 값 |
|--------|---|
| `AWS_ACCESS_KEY_ID` | GHA 측 IAM 사용자 — ECR push 권한 |
| `AWS_SECRET_ACCESS_KEY` | 동일 |
| `SSH_HOST` | EC2 EIP |
| `SSH_USER` | `ec2-user` |
| `SSH_PRIVATE_KEY` | EC2 PEM 파일 전체 (BEGIN/END 라인 포함) |

GHA IAM 사용자 권한 (최소): `AmazonEC2ContainerRegistryFullAccess`, 또는 좁힌 정책으로 `ecr:GetAuthorizationToken`, `ecr:BatchCheckLayerAvailability`, `ecr:Put*`, `ecr:Initiate*`, `ecr:Upload*`, `ecr:Complete*`.

## GitHub Environment

`kikoai/ai` repo → Settings → Environments → **`portal-dev`** 등록. 워크플로우의 `environment: portal-dev` 와 일치.

(필요 시) Required reviewers / branch protection 추가.

## 사전 준비 (1회성)

```bash
# 1. ECR 리포 생성
aws ecr create-repository --repository-name portal/dev/ai \
  --image-tag-mutability MUTABLE --image-scanning-configuration scanOnPush=true \
  --region ap-northeast-2 --profile kiko.ai

# 2. EC2 에 IAM Role 부착 (ECR pull용)
#    상세: aws-infra/kiko-ai-servers/portal-ai/CICD.md

# 3. EC2 에 deploy.ai.sh + docker-compose.yml + config/ 배치
#    상세: docs/infra/deployment.md

# 4. kikoai/ai repo Secrets / Environment 등록 (위 표)
```

## 트러블슈팅

| 증상 | 원인 / 조치 |
|------|------------|
| `ECR push: denied` | GHA IAM 사용자 권한 부족 |
| EC2 ECR pull 실패 | 인스턴스 IAM Role 미부착 — `aws sts get-caller-identity` 검증 |
| Health check 503 | 정상 (Modal stopped 또는 Langfuse 키 미설정 시 일부 disconnected). `curl -H "X-Internal-Token: ..." /health/ready` 로 어느 의존이 끊겼는지 확인 |
| ssh-action 타임아웃 | EC2 보안그룹 22번 포트 개방 확인 / GHA runner IP 화이트리스트 |
| 빌드 매우 느림 | 첫 빌드 ~10분, GHA 캐시 적중 후 ~3분. 캐시 안 잡히면 `cache-from: type=gha` 설정 확인 |
| concurrency cancel | dev 브랜치에 push 가 빠르게 연달아 들어오면 이전 deploy 가 직렬 대기 (cancel-in-progress: false) — 의도됨 |
| fork PR 머지 시 deploy 미실행 | 가드(`head.repo.full_name == github.repository`)가 차단 — 의도됨. 외부 contributor 코드는 owner 가 main 으로 직접 cherry-pick |

## 워크플로우 동기화

`aws-infra/.github/workflows/portal/*.yml` 가 SoT. `kikoai/ai/.github/workflows/*.yml` 은 같은 내용을 복사.

변경 시:
1. aws-infra 측 수정
2. `cp aws-infra/.github/workflows/portal/{ci.yml,deploy-dev.yml} kikoai/ai/.github/workflows/`
3. 양쪽 같이 커밋 (본 repo + aws-infra repo PR 별도)
