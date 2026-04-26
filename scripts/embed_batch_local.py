"""FashionSigLIP 로컬 배치 임베딩.

DB의 products 중 embedding이 NULL 인 항목을 모아 로컬 머신에서 FashionSigLIP 으로
인코딩한 뒤 `bulk_update_product_embeddings` RPC 로 일괄 upsert.

Apple Silicon Mac 은 MPS 자동 사용 — CPU 대비 5~10배 빠름.
80k 기준 예상 소요:
  - M-series MPS:  2~3 시간
  - x86 CPU only:  10~20 시간

사용:
    # 첫 1회 — torch / open_clip / Pillow / tqdm 설치 (~5GB)
    uv sync --group embed

    # 풀 배치
    uv run python scripts/embed_batch_local.py

    # 옵션
    uv run python scripts/embed_batch_local.py --limit 50           # 50개만 (테스트)
    uv run python scripts/embed_batch_local.py --batch-size 32      # GPU 배치 크기
    uv run python scripts/embed_batch_local.py --download-workers 16
    uv run python scripts/embed_batch_local.py --dry-run            # upsert 직전 멈춤

환경변수: `.env` 의 `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` 자동 로드.

재실행 안전 — `embedding IS NULL` 만 가져오므로 중단되어도 다음 실행 시 이어서 진행.
"""

import argparse
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 프로젝트 root 를 sys.path 에 추가 — scripts/ 에서 실행 시 app.* import 가능
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from app.core.config import settings  # noqa: E402

MODEL_ID = "Marqo/marqo-fashionSigLIP"
PAGE_SIZE = 200
UPSERT_CHUNK = 25  # HNSW 인덱스 유지비용 — chunk 크면 Supabase statement_timeout(~8s) 초과
UPSERT_MIN_CHUNK = 5  # 자동 분할 하한


def detect_device() -> str:
    """MPS (Apple Silicon) > CUDA > CPU."""
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


def fetch_pending(sb, limit: int | None = None) -> list[dict]:
    """미임베딩 products 페이지네이션 수집."""
    rows: list[dict] = []
    offset = 0
    while True:
        q = (
            sb.table("products")
            .select("id, images")
            .is_("embedding", "null")
            .not_.is_("images", "null")
            .range(offset, offset + PAGE_SIZE - 1)
        )
        res = q.execute()
        page = res.data or []
        if not page:
            break
        rows.extend(page)
        offset += PAGE_SIZE
        if limit and len(rows) >= limit:
            return rows[:limit]
        print(f"[fetch] 누적 {len(rows)} ...")
    return rows


def download_image(client: httpx.Client, url: str) -> Image.Image:
    r = client.get(url, timeout=15.0)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def download_batch(rows: list[dict], workers: int) -> dict[str, Image.Image]:
    """병렬 다운로드. 실패한 이미지는 결과에서 제외."""
    out: dict[str, Image.Image] = {}
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
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


def upsert(sb, embeddings: dict[str, list[float]], dry_run: bool = False, chunk_size: int = UPSERT_CHUNK) -> int:
    """timeout(57014) 감지 시 chunk_size 자동 절반 → 최소 UPSERT_MIN_CHUNK 까지 시도."""
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
            res = sb.rpc("bulk_update_product_embeddings", {"payload": chunk}).execute()
            n = res.data if isinstance(res.data, int) else 0
            total += n
            i += cur_chunk
        except Exception as e:
            err = str(e)
            if ("57014" in err or "statement timeout" in err) and cur_chunk > UPSERT_MIN_CHUNK:
                new_size = max(UPSERT_MIN_CHUNK, cur_chunk // 2)
                print(f"  [timeout] chunk {cur_chunk} 실패 → {new_size} 로 재시도")
                cur_chunk = new_size
                continue
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
        help=f"Supabase RPC 1회 upsert row 수 (default {UPSERT_CHUNK} — HNSW 인덱스 timeout 자동 감소)",
    )
    ap.add_argument("--dry-run", action="store_true", help="upsert 직전 중단 — 검증용")
    args = ap.parse_args()

    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        print("ERROR: .env 의 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 미설정")
        sys.exit(1)

    # supabase sync client 사용 — async 불필요 (단일 스레드 배치)
    from supabase import create_client

    sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)

    device = detect_device()
    model, preprocess = load_model(device)

    print("[fetch] 미임베딩 products 조회...")
    pending = fetch_pending(sb, limit=args.limit)
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

        n = upsert(sb, embs, dry_run=args.dry_run, chunk_size=args.upsert_chunk)
        upserted_total += n

        elapsed = time.time() - page_start
        done = offset + page_n
        overall_rate = done / (time.time() - start)
        eta = (total - done) / overall_rate if overall_rate > 0 else 0
        print(f"  upsert:   {n}    | page {elapsed:.1f}s | 전체 {done}/{total} | ETA {fmt_eta(eta)}")

    elapsed = time.time() - start
    print(f"\n✅ 완료 — upsert {upserted_total}/{total} (다운로드 실패 {failed_total}) · {fmt_eta(elapsed)}")

    cov = sb.from_("product_embedding_coverage").select("*").execute()
    if cov.data:
        print("\n[coverage 검증]")
        for row in cov.data:
            print(f"  {row['platform']:30s}  {row['embedded']:>6}/{row['total']:<6}  ({row['pct_embedded']}%)")


if __name__ == "__main__":
    main()
