# `sql/` — public schema SQL 관리

`migrations/versions/*.py` (alembic) 는 **`ai.*` 스키마** 만 관리. `public.search_products_v6` 처럼 검색 파이프라인이 의존하는 public 함수/뷰는 여기서 관리.

## 배포 방법

**배포 시 자동 적용** (7/10 사고 재발 방지). `Dockerfile` 이 `sql/` 를 이미지에 굽고
`postgresql-client`(psql) 를 설치 → `deploy.ai.sh` 가 alembic 단계 뒤에서
`docker compose run --rm --no-deps ai-server sh -c '...psql -f sql/functions/*.sql...'`
로 컨테이너 재기동 전에 적용. 이미지 안 sql 이 SOURCE OF TRUTH 이므로 파일만 고치면
다음 배포가 반영한다. 각 파일은 **모든 오버로드 DROP 후 재생성**(BEGIN/COMMIT 원자) 이라
시그니처가 바뀌어도 옛 오버로드가 남아 RPC 가 엉뚱하게 resolve 되는 사고를 근본 차단한다.

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
