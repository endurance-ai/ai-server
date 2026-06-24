"""FashionSigLIP 로컬 배치 임베딩 — dev-app Postgres 직접 접속 버전.

기존 `portal/ai/scripts/embed_batch_local.py` 의 dev-app 대응판.
Supabase REST 의존을 제거하고 psycopg 로 직접 SELECT + RPC 호출.

DB 의 products 중 embedding 이 NULL 인 항목을 모아 로컬 머신에서 FashionSigLIP 으로
인코딩한 뒤 `bulk_update_product_embeddings` RPC 로 일괄 upsert.

Apple Silicon Mac 은 MPS 자동 사용 — CPU 대비 5~10배 빠름.
71,850 기준 예상 소요:
  - M-series MPS:  2~3 시간
  - x86 CPU only:  10~20 시간

사용:
    cd /Users/hansangho/Desktop/portal/ai

    # 1) embed 그룹 동기화 (psycopg 추가됨 — pyproject.toml 패치 후)
    uv sync --group embed

    # 2) DSN export (사용자 IP 가 SG 5432 에 등록되어 있어야 함)
    export KIKOAI_DEVAPP_DSN='postgresql://app_user:<APP_USER_PASSWORD>@54.116.104.193:5432/kikoai?sslmode=require'

    # 3) dry-run 으로 50개만 검증
    uv run python scripts/embed_batch_devapp.py --limit 50 --dry-run

    # 4) 실배치 50개 (~30s)
    uv run python scripts/embed_batch_devapp.py --limit 50

    # 5) 풀 배치 (71,850 row, ~2~3h MPS)
    uv run python scripts/embed_batch_devapp.py

    # 옵션
    --batch-size 32         # GPU/MPS 배치 크기
    --download-workers 16   # 이미지 다운로드 동시성
    --upsert-chunk 25       # RPC 1회 upsert row 수

재실행 안전 — `embedding IS NULL` 만 가져오므로 중단되어도 다음 실행 시 이어서 진행.
"""

import argparse
import io
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import psycopg
from PIL import Image
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MODEL_ID = "Marqo/marqo-fashionSigLIP"
PAGE_SIZE = 200
UPSERT_CHUNK = 25
UPSERT_MIN_CHUNK = 5


def detect_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def load_model(device: str):
    import open_clip

    print(f"[model] loading {MODEL_ID} on {device}...")
    model, _, preprocess = open_clip.create_model_and_transforms(f"hf-hub:{MODEL_ID}")
    model = model.to(device).eval()
    return model, preprocess


def fetch_pending(conn: psycopg.Connection, limit: int | None) -> list[dict]:
    """미임베딩 products 일괄 수집. 71,850 row 기준 ~2초."""
    # pending = product_embeddings 에 row 가 없는 products (anti-join).
    # 구 `products.embedding IS NULL` 센티넬은 migration 086 에서 컬럼 drop 됨 —
    # product_embeddings(071) 가 임베딩 단일 출처라 그 부재로 pending 판별.
    # 대표 이미지는 image_url == images[0] (전 데이터셋에서 동일 확인). images 배열이
    # 비어있어도(예: Zara 엔진은 image_url 만 채움) image_url 로 폴백해 임베딩 대상에 포함.
    sql = """
        SELECT p.id,
               CASE WHEN p.images IS NOT NULL AND array_length(p.images, 1) > 0
                    THEN p.images
                    ELSE ARRAY[p.image_url]
               END AS images
        FROM products p
        WHERE NOT EXISTS (
                SELECT 1 FROM product_embeddings pe WHERE pe.product_id = p.id
              )
          AND (
                (p.images IS NOT NULL AND array_length(p.images, 1) > 0)
                OR p.image_url ~ '^https?://'
              )
        ORDER BY p.id
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    # UUID → str (psycopg Json dumps 용)
    return [{"id": str(r["id"]), "images": r["images"]} for r in rows]


def download_image(client: httpx.Client, url: str) -> Image.Image:
    r = client.get(url, timeout=15.0)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def download_batch(rows: list[dict], workers: int) -> dict[str, Image.Image]:
    out: dict[str, Image.Image] = {}
    # 일부 CDN(예: static.zara.net)은 브라우저 User-Agent 없는 요청을 403 차단한다.
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures: dict = {}
            for r in rows:
                imgs = r.get("images") or []
                if not imgs:
                    continue
                fut = ex.submit(download_image, client, imgs[0])
                futures[fut] = r["id"]
            for fut in as_completed(futures):
                pid = futures[fut]
                try:
                    out[pid] = fut.result()
                except Exception as e:
                    print(f"  [skip] {pid}: {type(e).__name__}: {str(e)[:80]}")
    return out


def encode_batch(
    model, preprocess, images: dict[str, Image.Image], batch_size: int, device: str
) -> dict[str, list[float]]:
    import torch
    import torch.nn.functional as F  # noqa: N812

    embs: dict[str, list[float]] = {}
    ids = list(images.keys())
    pils = list(images.values())
    for i in range(0, len(pils), batch_size):
        chunk_pils = pils[i : i + batch_size]
        chunk_ids = ids[i : i + batch_size]
        tensor = torch.stack([preprocess(im) for im in chunk_pils]).to(device)
        with torch.inference_mode():
            feats = model.encode_image(tensor)
            feats = F.normalize(feats, dim=-1)
        for pid, vec in zip(chunk_ids, feats.cpu().tolist(), strict=True):
            embs[pid] = vec
    return embs


def to_pgvector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


def upsert(
    conn: psycopg.Connection,
    embeddings: dict[str, list[float]],
    dry_run: bool,
    chunk_size: int,
) -> int:
    """timeout/오류 시 chunk_size 절반으로 자동 재시도, 최소 UPSERT_MIN_CHUNK 까지."""
    payload = [{"id": pid, "embedding": to_pgvector(e), "model": MODEL_ID} for pid, e in embeddings.items()]
    if dry_run:
        print(f"  [dry-run] would upsert {len(payload)} rows")
        return len(payload)

    total = 0
    i = 0
    cur_chunk = chunk_size
    while i < len(payload):
        chunk = payload[i : i + cur_chunk]
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT bulk_update_product_embeddings(%s)", [Jsonb(chunk)])
                row = cur.fetchone()
                n = row[0] if row else 0
            conn.commit()
            total += n
            i += cur_chunk
        except (psycopg.errors.QueryCanceled, psycopg.errors.OperationalError) as e:
            conn.rollback()
            if cur_chunk > UPSERT_MIN_CHUNK:
                new_size = max(UPSERT_MIN_CHUNK, cur_chunk // 2)
                print(f"  [retry] chunk {cur_chunk} 실패({type(e).__name__}) → {new_size} 로 재시도")
                cur_chunk = new_size
                continue
            raise
        except Exception:
            conn.rollback()
            raise
    return total


def fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.2f}h"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="N 개만 처리 (테스트용)")
    ap.add_argument("--batch-size", type=int, default=16, help="GPU/MPS 배치 크기 (default 16)")
    ap.add_argument("--download-workers", type=int, default=8, help="이미지 다운로드 동시성 (default 8)")
    ap.add_argument(
        "--upsert-chunk",
        type=int,
        default=UPSERT_CHUNK,
        help=f"RPC 1회 upsert row 수 (default {UPSERT_CHUNK})",
    )
    ap.add_argument("--dry-run", action="store_true", help="upsert 직전 중단 — 검증용")
    args = ap.parse_args()

    dsn = os.environ.get("KIKOAI_DEVAPP_DSN")
    if not dsn:
        print("ERROR: 환경변수 KIKOAI_DEVAPP_DSN 미설정")
        print('예) export KIKOAI_DEVAPP_DSN="postgresql://app_user:PASS@54.116.104.193:5432/kikoai?sslmode=require"')
        sys.exit(1)

    print("[db] connecting...")
    conn = psycopg.connect(dsn, application_name="embed_batch_devapp")

    try:
        device = detect_device()
        model, preprocess = load_model(device)

        print("[fetch] 미임베딩 products 조회...")
        pending = fetch_pending(conn, limit=args.limit)
        total = len(pending)
        print(f"[fetch] {total} 건 처리 예정")
        if total == 0:
            print("처리할 항목 없음")
            return

        start = time.time()
        upserted_total = 0
        failed_total = 0
        page_count = (total + PAGE_SIZE - 1) // PAGE_SIZE

        for offset in range(0, total, PAGE_SIZE):
            page = pending[offset : offset + PAGE_SIZE]
            page_n = len(page)
            page_idx = offset // PAGE_SIZE + 1
            page_start = time.time()

            print(f"\n=== page {page_idx}/{page_count} ({page_n} 건) ===")
            images = download_batch(page, args.download_workers)
            downloaded = len(images)
            failed = page_n - downloaded
            failed_total += failed
            print(f"  download: {downloaded}/{page_n}  (fail {failed})")

            if not images:
                continue

            encode_start = time.time()
            embs = encode_batch(model, preprocess, images, args.batch_size, device)
            encode_dur = time.time() - encode_start
            print(f"  encode:   {len(embs)} on {device} — {encode_dur:.1f}s ({len(embs) / encode_dur:.1f}/s)")

            n = upsert(conn, embs, dry_run=args.dry_run, chunk_size=args.upsert_chunk)
            upserted_total += n

            elapsed = time.time() - page_start
            done = offset + page_n
            overall_rate = done / (time.time() - start)
            eta = (total - done) / overall_rate if overall_rate > 0 else 0
            print(f"  upsert:   {n}    | page {elapsed:.1f}s | 전체 {done}/{total} | ETA {fmt_eta(eta)}")

        elapsed = time.time() - start
        print(f"\n완료 — upsert {upserted_total}/{total} (다운로드 실패 {failed_total}) · {fmt_eta(elapsed)}")

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM product_embedding_coverage")
            rows = cur.fetchall()
        if rows:
            print("\n[coverage 검증]")
            for row in rows:
                plat = row.get("platform", "?")
                emb = row.get("embedded", 0)
                tot = row.get("total", 0)
                pct = row.get("pct_embedded", 0)
                print(f"  {plat:30s}  {emb:>6}/{tot:<6}  ({pct}%)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
