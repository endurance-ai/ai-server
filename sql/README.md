# `sql/` — public schema SQL 관리

`migrations/versions/*.py` (alembic) 는 **`ai.*` 스키마** 만 관리. `public.search_products_v6` 처럼 검색 파이프라인이 의존하는 public 함수/뷰는 여기서 관리.

## 배포 방법

alembic 자동 적용 대상이 아님. 아래 중 하나로 수동 적용:

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
