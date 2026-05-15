# 배포

> EC2 t4g.medium (ARM) + Docker Compose 5컨테이너 + Modal serverless. 인프라 측 SoT 는 `aws-infra/kiko-ai-servers/portal-ai/`.

## 토폴로지

```
[GitHub Actions]                   [AWS]                                   [Modal]
─────────────────                  ─────────────────────────────           ─────────────
PR → ci.yml (검증)                 ECR: portal/dev/ai                      portal-embed
merge → deploy-dev.yml             EC2 t4g.medium (ap-northeast-2)         (T4 GPU)
  └─ ECR push                        └─ docker-compose                     scale-to-zero
  └─ SSH deploy.ai.sh                    ├─ ai-server (8000)
                                         ├─ litellm + litellm-db (4000)
                                         └─ langfuse-web + langfuse-db (3000)
```

## EC2 스택

`aws-infra/kiko-ai-servers/portal-ai/docker/docker-compose.yml` 의 5컨테이너:

| 컨테이너 | 역할 | 포트 | 메모리 |
|---------|------|------|-------|
| `ai-server` | FastAPI (이 프로젝트) | 8000 | 512M |
| `litellm` | LiteLLM proxy | 4000 | 1024M |
| `litellm-db` | LiteLLM 메타 (Postgres) | (내부) | 256M |
| `langfuse-web` | Langfuse UI/API | 3000 | 512M |
| `langfuse-db` | Langfuse 데이터 (Postgres) | (내부) | 512M |
| **합계** | | | **~2.8GB** (4GB 캡 내) |

> AI 서버 자체는 stateless. 영속 데이터는 Supabase + Langfuse Postgres 만.

## EC2 정보

| 항목 | 값 |
|------|---|
| AWS 프로필 | `kiko.ai` |
| Instance ID | `i-095a9f3b60b2bb73f` |
| 타입 | t4g.medium (또는 t4g.large) |
| 리전 | `ap-northeast-2` |
| OS | Amazon Linux 2023 ARM |
| EIP | (운영 시 부착) |

## 1회성 셋업

```bash
# 로컬에서 EC2 SSM 접속
aws ssm start-session --target i-095a9f3b60b2bb73f --profile kiko.ai --region ap-northeast-2

# EC2 안에서
sudo dnf install -y docker
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user

# Docker Compose v2 설치 (ARM)
sudo curl -L "https://github.com/docker/compose/releases/download/v2.29.1/docker-compose-linux-aarch64" \
  -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

자동화: `aws-infra/kiko-ai-servers/portal-ai/scripts/setup.sh`.

## 파일 배치 (EC2)

```
/home/ec2-user/
├── .env                         # aws-infra/.../env/.env 에서 복사
├── docker-compose.yml           # aws-infra/.../docker/docker-compose.yml
├── config/
│   └── litellm.yaml             # aws-infra/.../config/litellm.yaml
└── scripts/
    └── deploy.ai.sh             # aws-infra/.../scripts/deploy.ai.sh (chmod +x)
```

스택 기동:

```bash
cd /home/ec2-user
docker compose up -d
docker compose logs -f
```

## 첫 배포 절차

1. **Supabase migration 적용** — `kikoai/app/supabase/migrations/030_search_products_v5.sql`
2. **Modal `/embed` 배포** — `aws-infra/kiko-ai-servers/portal-ai/modal/embed_app.py` (`modal deploy`)
3. **EC2 docker compose up** — Langfuse + LiteLLM + 빈 ai-server (이미지 미존재 → ai-server 만 fail)
4. **Langfuse 첫 회원가입** → 프로젝트 `kiko.ai` 생성 → API Keys 발급 → `.env` 채움
5. **GHA secrets 등록** — `AWS_*`, `SSH_*` (상세: [`cicd.md`](cicd.md))
6. **dev 브랜치 첫 커밋 push** → CI 통과 → merge → deploy-dev.yml 실행 → EC2 ai-server 띄워짐
7. **Vercel env 등록** — `AI_SERVER_URL=http://<EIP>:8000`, `INTERNAL_API_TOKEN=<token>`

## Modal 배포 (별도)

```bash
cd aws-infra/kiko-ai-servers/portal-ai/modal
modal token new                        # 1회
modal secret create portal-ai-modal \
  EMBED_AUTH_TOKEN=$(openssl rand -hex 32)
modal deploy embed_app.py
```

배포 후 출력 URL 을 EC2 `.env` 의 `MODAL_EMBED_URL` 에 등록. `EMBED_AUTH_TOKEN` 값은 `MODAL_EMBED_TOKEN` 과 **동일하게** 채움.

상세: `aws-infra/kiko-ai-servers/portal-ai/modal/README.md`.

### Modal 정책

| 환경 | `min_containers` | `scaledown_window` |
|------|-----------------|-------------------|
| dev (현재) | 0 (scale-to-zero) | 300s (5분) |
| prod (필요 시) | 1 (warm 1대 상시) | — |

scale-to-zero 시 cold start ~10~17초. 트래픽 패턴 보면서 조절.

## 폴백 / 장애 대응

| 시나리오 | 대응 |
|---------|------|
| AI 서버 5xx / down | Vercel 의 `AI_SERVER_TIMEOUT_MS=8000` 적용 → Next.js 가 v4 (`/api/search-products`) in-process 폴백 |
| Modal 다운 | AI 서버 502 → 동일 폴백 |
| Supabase RPC 다운 | 동일 폴백 |
| Langfuse 다운 | LiteLLM/AI 서버 정상 동작 (callback fail-safe) — trace 만 손실 |
| EC2 down | DNS 절체 (Vercel 측 `AI_SERVER_URL` 미설정 시 자동 v4 폴백) |

## 모니터링

- Langfuse Web — LLM/embedding/파이프라인 trace
- `docker compose logs -f ai-server` — uvicorn 로그
- `docker compose ps` — 헬스체크 상태
- Modal 대시보드 — GPU 사용률 + 콜드스타트 빈도

## 비용 추정 (월, POC 트래픽 100/day)

| 항목 | 비용 |
|------|------|
| EC2 t4g.medium 24/7 | ~$30 |
| EC2 t4g.large 24/7 | ~$60 |
| EBS gp3 30GB | ~$3 |
| Modal T4 (scale-to-zero, 호출당 1초) | ~$1~5 |
| Supabase Pro | (별도, kikoai/app 과 공유) |
| **합계 (medium)** | **~$35~40** |

## 관련 문서

- [`cicd.md`](cicd.md) — GitHub Actions + ECR + SSH 배포 상세
- [`env.md`](env.md) — 환경변수 매트릭스
- `aws-infra/kiko-ai-servers/portal-ai/CICD.md` — 인프라 측 셋업 가이드 (SSM/IAM/ECR)
- `aws-infra/kiko-ai-servers/portal-ai/modal/README.md` — Modal 배포

## SPEC-ONBOARD-CARDS-001 — Manual Smoke (Scenario e)

DoD §11.4 scenario (e) requires a **real Apify board URL scrape** which cannot
be reproduced in CI (Apify creds + live network). Verify during cutover:

1. Set `APIFY_TOKEN` in dev `.env` (token from Apify console, redact in logs).
2. Confirm startup log line `🎨 [APIFY] provider armed actor=epctex/pinterest-scraper token_len=…`.
3. In Telegram dev bot, complete onboarding stages 1–3.
4. At Stage 4 (Pinterest card), tap `[URL 보낼게요]` then paste a real board URL
   e.g. `https://www.pinterest.com/user/board-name/`.
5. Observe:
   - Webhook log: `📥 [webhook] 🎟 command=` absent (text message path).
   - Apify outbound request completes within 30s (`ApifyTimeoutError` not raised).
   - Bot replies with completion message + non-zero pin count in stage span metadata.
6. Issue `/recommend` with a fashion photo and confirm boost weight applied
   (search results favor mood + color from Stage 1–2 plus Pinterest brand cues).

Cutover order (plan §1.3):

1. Apply Alembic migrations to dev-app Postgres:
   ```bash
   uv run alembic upgrade head
   # 0003_create_log_conversation_event  — ai.log_conversation_event + 4 indexes
   # 0004_add_onboarded_at               — user_session onboarded_at + 7 cols
   ```
2. Deploy this codebase with `PINTEREST_BOOTSTRAP_ENABLED=true` and
   `ONBOARDING_CARDS_ENABLED=true`.
3. Run smoke 1–6 above.
4. Roll forward to production once smoke passes.
