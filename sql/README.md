# `sql/` — public schema SQL 관리

`public.search_products_v6` 처럼 검색 파이프라인이 의존하는 public 함수/뷰는 여기서 관리.

## DB 경로 2개 (같은 `kikoai` DB, 다른 스키마)

| | Path 1 | Path 2 (여기) |
|---|---|---|
| **뭐** | `ai.*` 내 테이블 | `public` 검색 RPC 함수 |
| **어디에** | `migrations/versions/*.py` | `sql/functions/*.sql` |
| **적용** | deploy.ai.sh 의 alembic | CI `apply-db-functions` (dev-app exec) |

- 내 테이블/컬럼 → `uv run alembic revision` → py 편집
- 검색 함수 → `sql/functions/*.sql` 편집 (반드시 `BEGIN; 오버로드 DROP; CREATE; COMMIT` 패턴)
- 둘 다 **ai-server dev 머지 하나로 자동 적용**. 수동 psql 금지(=7/10 원인). public 함수를 alembic 에 넣지 말 것(alembic 은 `ai_user` 라 public DDL 권한 없음).

## 배포 방법

**dev 머지 시 자동 적용** (7/10 사고 재발 방지). ai-server CI(`deploy-dev.yml`)의
`apply-db-functions` job 이 `sql/functions/*.sql` 를 dev-app 으로 scp → dev-app `db`
컨테이너에 로컬 exec(`docker exec -i db psql`, superuser peer/trust 무비번)으로 적용.
정본(이 디렉토리)이 SOURCE OF TRUTH 이므로 파일만 고쳐 머지하면 반영된다. 각 파일은
**모든 오버로드 DROP 후 재생성**(BEGIN/COMMIT 원자) 이라 시그니처가 바뀌어도 옛 오버로드가
남아 RPC 가 엉뚱하게 resolve 되는 사고를 근본 차단한다.

수동 적용이 필요할 때(핫픽스 등) 는 아래 중 하나:

**옵션 1 — DBeaver (개발자 개인 세션)**
1. dev-app Postgres 커넥션 열기 (superuser 롤 필요)
2. `sql/functions/*.sql` 파일 열기
3. 전체 실행

**옵션 2 — dev-app EC2 SSH**
```bash
ssh -i ~/Desktop/kikoai-dev-servers/kikoai-key.pem ec2-user@<dev-app-eip>
sudo -u postgres psql -d kikoai -f /path/to/sql/functions/search_products_v6.sql
```

## 검증

적용 후:
```sql
\df search_products_v6
```
로 시그니처에 새 파라미터가 나오면 성공.

## 파일 목록

| 파일 | 설명 |
|---|---|
| `functions/search_products_v6.sql` | 메인 검색 RPC. embedding-first, family gate + optional color filter |
