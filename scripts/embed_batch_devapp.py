"""FashionSigLIP 로컬 배치 임베딩 — dev-app Postgres 직접 접속 버전.

기존 `portal/ai/scripts/embed_batch_local.py` 의 dev-app 대응판.
Supabase REST 의존을 제거하고 psycopg 로 직접 SELECT + RPC 호출.

DB 의 활성 products 중 product_embeddings row 가 없고 정상 `image_url` 을 가진 항목을
모아 로컬 머신에서 FashionSigLIP 으로 인코딩한 뒤 일괄 upsert.

`image_url` 이 대표 이미지의 단일 출처다. 영구적으로 깨진 대표 URL은 DB의 다른 이미지
후보와 상품 페이지에서 복구하고, 원자적 repair RPC로 모든 이미지 필드를 정리한 뒤에만
대체 이미지를 임베딩한다. 일시 오류와 복구 불가 상태는 product_image_failures 에 기록해
같은 URL을 매 실행마다 다시 요청하지 않는다.

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

재실행 안전 — product_embeddings anti-join과 이미지 실패 상태로 중단 지점부터 이어진다.

배치 완료 시 DB 역할에 권한이 있으면 product_crawl_status(091, brand_node_id 기준)와
product_crawl_runs 에 반영한다. 권한이 없는 임베딩 전용 역할에서는 경고 후 건너뛰며,
임베딩 성공 여부에는 영향을 주지 않는다. brand_node_id 가 NULL인 상품은 집계에서
자연히 제외되고, 이미 'active' 인 브랜드는 'embedded' 로 되돌리지 않는다.
--dry-run 시에는 동기화하지 않는다.
"""

import argparse
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import psycopg
from PIL import Image, UnidentifiedImageError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MODEL_ID = "Marqo/marqo-fashionSigLIP"
PAGE_SIZE = 200
UPSERT_CHUNK = 25
UPSERT_MIN_CHUNK = 5
IMAGE_TIMEOUT = 15.0
RETRY_DELAY = timedelta(hours=6)


class NonImageResponseError(ValueError):
    """The URL returned a successful response that was not an image."""


@dataclass(frozen=True)
class ImageFailure:
    url: str
    kind: str
    disposition: str
    http_status: int | None
    error: str
    next_retry_at: datetime | None


@dataclass(frozen=True)
class ImageRepair:
    product_id: str
    before_url: str
    replacement_url: str | None
    source_image_url: str | None
    images: list[str]
    bad_urls: list[str]
    mark_out_of_stock: bool


@dataclass
class DownloadOutcome:
    product_id: str
    canonical_url: str
    image: Image.Image | None = None
    repair: ImageRepair | None = None
    failure: ImageFailure | None = None


class ProductImageMetaParser(HTMLParser):
    """Collect only explicit representative-image metadata from a product page."""

    def __init__(self, page_url: str) -> None:
        super().__init__()
        self.page_url = page_url
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        candidate: str | None = None
        if tag.lower() == "meta" and values.get("property", "").lower() in {
            "og:image",
            "og:image:secure_url",
        }:
            candidate = values.get("content")
        elif tag.lower() == "link" and "image_src" in values.get("rel", "").lower().split():
            candidate = values.get("href")
        if candidate:
            self.urls.append(urljoin(self.page_url, candidate))


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


def fetch_pending(
    conn: psycopg.Connection,
    limit: int | None,
    platforms: list[str] | None = None,
) -> list[dict]:
    """활성·대표 이미지 보유 미임베딩 products 일괄 수집."""
    # pending = product_embeddings 에 row 가 없는 products (anti-join).
    # 구 `products.embedding IS NULL` 센티넬은 migration 086 에서 컬럼 drop 됨 —
    # product_embeddings(071) 가 임베딩 단일 출처라 그 부재로 pending 판별.
    # 대표 이미지 SOT 는 products.image_url 이다. images[0] 은 호환용 mirror일 뿐
    # 임베딩 입력을 결정하지 않는다. 품절 상품은 검색 RPC가 노출하지 않으므로 제외하고,
    # 재입고 시 in_stock=true + embedding 부재 조건으로 자동 복귀한다.
    # 같은 canonical URL 의 영구 실패는 격리하고 retryable 실패만 예약 시각 이후 재시도한다.
    sql = """
        SELECT p.id,
               p.brand_node_id,
               p.image_url,
               p.source_image_url,
               p.images,
               p.product_url,
               COALESCE(pif.attempt_count, 0) AS image_failure_attempts
        FROM products p
        LEFT JOIN product_image_failures pif
          ON pif.product_id = p.id
         AND pif.failed_url = p.image_url
        WHERE NOT EXISTS (
                SELECT 1 FROM product_embeddings pe WHERE pe.product_id = p.id
              )
          AND p.in_stock = true
          AND p.image_url ~ '^https?://'
          AND (
                pif.product_id IS NULL
                OR (
                  pif.disposition = 'retryable'
                  AND pif.next_retry_at <= now()
                )
              )
        {platform_filter}
        ORDER BY p.id
    """
    platform_filter = ""
    params: list[object] = []
    if platforms:
        platform_filter = "AND p.platform = ANY(%s)"
        params.append(platforms)
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql.format(platform_filter=platform_filter), params)
        rows = cur.fetchall()
    return [{**r, "id": str(r["id"])} for r in rows]


def download_image(client: httpx.Client, url: str) -> Image.Image:
    r = client.get(url, timeout=IMAGE_TIMEOUT)
    r.raise_for_status()
    content_type = (r.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if (
        content_type
        and not content_type.startswith("image/")
        and content_type
        not in {
            "application/octet-stream",
            "binary/octet-stream",
        }
    ):
        raise NonImageResponseError(f"unexpected content-type {content_type}")
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def classify_download_error(url: str, error: Exception, prior_attempts: int = 0) -> ImageFailure:
    status: int | None = None
    kind = "unknown"
    disposition = "retryable"
    retry_delay = RETRY_DELAY

    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in {404, 410}:
            kind = f"http_{status}"
            disposition = "permanent"
        elif status in {403, 429}:
            kind = f"http_{status}"
        elif 500 <= status <= 599:
            kind = "http_5xx"
        else:
            kind = "unknown"
    elif isinstance(error, httpx.TimeoutException):
        kind = "timeout"
    elif isinstance(error, httpx.RequestError):
        kind = "network"
    elif isinstance(error, NonImageResponseError):
        kind = "non_image"
        disposition = "permanent" if prior_attempts >= 1 else "retryable"
        retry_delay = timedelta(hours=24)
    elif isinstance(error, (UnidentifiedImageError, OSError)):
        kind = "decode"
        disposition = "permanent" if prior_attempts >= 1 else "retryable"
        retry_delay = timedelta(hours=24)

    return ImageFailure(
        url=url,
        kind=kind,
        disposition=disposition,
        http_status=status,
        error=f"{type(error).__name__}: {' '.join(str(error).split())[:500]}",
        next_retry_at=datetime.now(UTC) + retry_delay if disposition == "retryable" else None,
    )


def unique_http_urls(values: list[object], *, exclude: set[str] | None = None) -> list[str]:
    excluded = exclude or set()
    seen: set[str] = set()
    urls: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        url = value.strip()
        if not url.lower().startswith(("http://", "https://")) or url in excluded or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def extract_shopify_images(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    root = payload.get("product") if isinstance(payload.get("product"), dict) else payload
    values: list[object] = []

    def add(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            if isinstance(value.get("src"), str):
                values.append(value["src"])
            elif isinstance(value.get("url"), str):
                values.append(value["url"])

    for item in root.get("images", []) if isinstance(root.get("images"), list) else []:
        add(item)
    add(root.get("featured_image"))
    for media in root.get("media", []) if isinstance(root.get("media"), list) else []:
        if isinstance(media, dict):
            add(media.get("preview_image"))
    for variant in root.get("variants", []) if isinstance(root.get("variants"), list) else []:
        if isinstance(variant, dict):
            add(variant.get("featured_image"))
    return unique_http_urls(values)


def shopify_json_url(product_url: str) -> str:
    parts = urlsplit(product_url)
    path = parts.path.rstrip("/")
    if not path.endswith(".js"):
        path += ".js"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def discover_product_images(client: httpx.Client, row: dict) -> tuple[str, list[str]]:
    """Return (live|gone|retryable, authoritative representative candidates)."""
    product_url = str(row.get("product_url") or "")
    canonical_url = str(row.get("image_url") or "")

    if "cdn.shopify.com" in canonical_url:
        try:
            response = client.get(shopify_json_url(product_url), timeout=IMAGE_TIMEOUT)
            if response.status_code not in {404, 410}:
                response.raise_for_status()
                candidates = extract_shopify_images(response.json())
                if candidates:
                    return "live", candidates
        except (httpx.TimeoutException, httpx.RequestError, json.JSONDecodeError):
            pass
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {403, 429} or error.response.status_code >= 500:
                return "retryable", []

    try:
        response = client.get(product_url, timeout=IMAGE_TIMEOUT)
        if response.status_code in {404, 410}:
            return "gone", []
        response.raise_for_status()
        parser = ProductImageMetaParser(str(response.url))
        parser.feed(response.text)
        return "live", unique_http_urls(parser.urls)
    except (httpx.TimeoutException, httpx.RequestError):
        return "retryable", []
    except httpx.HTTPStatusError as error:
        if error.response.status_code in {404, 410}:
            return "gone", []
        return "retryable", []


def download_product_image(client: httpx.Client, row: dict) -> DownloadOutcome:
    pid = str(row["id"])
    canonical = str(row["image_url"])
    prior_attempts = int(row.get("image_failure_attempts") or 0)
    try:
        return DownloadOutcome(product_id=pid, canonical_url=canonical, image=download_image(client, canonical))
    except Exception as error:
        canonical_failure = classify_download_error(canonical, error, prior_attempts)

    if canonical_failure.disposition == "retryable":
        return DownloadOutcome(product_id=pid, canonical_url=canonical, failure=canonical_failure)

    bad_urls = {canonical}
    db_candidates = unique_http_urls(
        [*(row.get("images") or []), row.get("source_image_url")],
        exclude=bad_urls,
    )
    live_candidates: list[str] = []
    for candidate in db_candidates:
        try:
            image = download_image(client, candidate)
            clean_images = [candidate, *[url for url in db_candidates if url != candidate and url not in bad_urls]]
            source = row.get("source_image_url")
            if not isinstance(source, str) or source in bad_urls:
                source = candidate
            return DownloadOutcome(
                product_id=pid,
                canonical_url=canonical,
                image=image,
                repair=ImageRepair(pid, canonical, candidate, source, clean_images, sorted(bad_urls), False),
            )
        except Exception as error:
            failure = classify_download_error(candidate, error)
            if failure.disposition == "permanent":
                bad_urls.add(candidate)
            else:
                live_candidates.append(candidate)

    page_state, discovered = discover_product_images(client, row)
    if page_state == "retryable":
        retryable = ImageFailure(
            url=canonical,
            kind=canonical_failure.kind,
            disposition="retryable",
            http_status=canonical_failure.http_status,
            error=f"{canonical_failure.error}; product page verification unavailable",
            next_retry_at=datetime.now(UTC) + RETRY_DELAY,
        )
        return DownloadOutcome(product_id=pid, canonical_url=canonical, failure=retryable)

    discovered_candidates = unique_http_urls(discovered, exclude=bad_urls)
    for candidate in discovered_candidates:
        try:
            image = download_image(client, candidate)
            clean_images = [
                candidate,
                *unique_http_urls([*discovered_candidates, *live_candidates], exclude=bad_urls | {candidate}),
            ]
            return DownloadOutcome(
                product_id=pid,
                canonical_url=canonical,
                image=image,
                repair=ImageRepair(pid, canonical, candidate, candidate, clean_images, sorted(bad_urls), False),
            )
        except Exception as error:
            failure = classify_download_error(candidate, error)
            if failure.disposition == "permanent":
                bad_urls.add(candidate)

    repair = ImageRepair(
        product_id=pid,
        before_url=canonical,
        replacement_url=None,
        source_image_url=None,
        images=live_candidates,
        bad_urls=sorted(bad_urls),
        mark_out_of_stock=page_state == "gone",
    )
    terminal = ImageFailure(
        url=canonical,
        kind=canonical_failure.kind,
        disposition="permanent",
        http_status=canonical_failure.http_status,
        error=f"{canonical_failure.error}; no verified replacement ({page_state})",
        next_retry_at=None,
    )
    return DownloadOutcome(product_id=pid, canonical_url=canonical, repair=repair, failure=terminal)


def download_batch(rows: list[dict], workers: int) -> list[DownloadOutcome]:
    outcomes: list[DownloadOutcome] = []
    with httpx.Client(timeout=IMAGE_TIMEOUT, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(download_product_image, client, row): row for row in rows}
            for fut in as_completed(futures):
                row = futures[fut]
                pid = str(row["id"])
                try:
                    outcomes.append(fut.result())
                except Exception as e:
                    canonical = str(row.get("image_url") or "unknown")
                    fallback = classify_download_error(canonical, e)
                    outcomes.append(DownloadOutcome(product_id=pid, canonical_url=canonical, failure=fallback))
    return outcomes


def persist_download_outcomes(
    conn: psycopg.Connection,
    outcomes: list[DownloadOutcome],
    dry_run: bool,
) -> dict[str, Image.Image]:
    """Persist repair/failure state before allowing an image to be embedded."""
    images: dict[str, Image.Image] = {}
    for outcome in outcomes:
        repair_applied = outcome.repair is None
        if outcome.repair is not None:
            repair = outcome.repair
            payload = {
                "id": repair.product_id,
                "before_url": repair.before_url,
                "replacement_url": repair.replacement_url,
                "source_image_url": repair.source_image_url,
                "images": repair.images,
                "bad_urls": repair.bad_urls,
                "mark_out_of_stock": repair.mark_out_of_stock,
            }
            if dry_run:
                print(
                    f"  [dry-run repair] {outcome.product_id}: {repair.before_url[:60]}"
                    f" -> {(repair.replacement_url or 'NULL')[:60]}"
                )
                repair_applied = True
            else:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT repair_product_image_assets(%s)", [Jsonb([payload])])
                        row = cur.fetchone()
                        repair_applied = bool(row and row[0] == 1)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                if not repair_applied:
                    print(f"  [stale repair] {outcome.product_id}: canonical image changed concurrently")

        if outcome.failure is not None:
            failure = outcome.failure
            print(f"  [skip] {outcome.product_id}: {failure.kind} {failure.disposition} — {failure.error[:100]}")
            if not dry_run:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT record_product_image_failure(%s,%s,%s,%s,%s,%s,%s)",
                            [
                                int(outcome.product_id),
                                failure.url,
                                failure.kind,
                                failure.disposition,
                                failure.http_status,
                                failure.next_retry_at,
                                failure.error,
                            ],
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

        if outcome.image is not None and repair_applied:
            images[outcome.product_id] = outcome.image
            if outcome.repair is None and not dry_run:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT clear_product_image_failure(%s,%s)",
                            [int(outcome.product_id), outcome.canonical_url],
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
    return images


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


def sync_crawl_status(conn: psycopg.Connection, brand_embed_counts: dict[int, int]) -> None:
    """임베딩 결과를 product_crawl_status(091, brand_node_id 기준)에 반영한다.

    /admin/product-collection 이 읽는 product_crawl_brands 뷰의 소스가 이 테이블이라,
    임베딩 배치도 crawl.ts/import-products.ts 와 같은 패턴으로 동기화해야 admin에서
    수기 마킹 없이 embedded 상태가 보인다. products.brand_node_id 가 NULL인 상품
    (brand_nodes 미매칭)은 집계에서 이미 제외됨 — no-op.

    이미 'active' 인 브랜드는 되돌리지 않는다 (embedded 로 다운그레이드 방지).
    """
    if not brand_embed_counts:
        return
    try:
        with conn.cursor() as cur:
            for brand_node_id, count in brand_embed_counts.items():
                cur.execute(
                    """
                    INSERT INTO product_crawl_status (brand_node_id, status, embedded_at, qc_summary)
                    VALUES (%(brand_node_id)s, 'embedded', now(), %(qc_summary)s)
                    ON CONFLICT (brand_node_id) DO UPDATE SET
                        status = CASE WHEN product_crawl_status.status = 'active'
                                      THEN product_crawl_status.status
                                      ELSE 'embedded' END,
                        embedded_at = now(),
                        qc_summary = product_crawl_status.qc_summary || %(qc_summary)s
                    """,
                    {"brand_node_id": brand_node_id, "qc_summary": Jsonb({"embedded_count": count})},
                )
                cur.execute(
                    """
                    INSERT INTO product_crawl_runs (brand_node_id, stage, status, actor, command, metrics)
                    VALUES (%(brand_node_id)s, 'embed', 'success', 'embed-batch-devapp',
                            'embed_batch_devapp.py', %(metrics)s)
                    """,
                    {"brand_node_id": brand_node_id, "metrics": Jsonb({"embedded_count": count})},
                )
            conn.commit()
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        print("\n[sync] 권한 없음 — 선택적 product_crawl_status 동기화를 건너뜀")
        return
    print(f"\n[sync] product_crawl_status 갱신 — {len(brand_embed_counts)}개 브랜드")


def fmt_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.2f}h"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--platform",
        default=None,
        help="comma-separated platform keys to restrict embedding (for example samostuff,teak)",
    )
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
        platforms = [p.strip() for p in args.platform.split(",") if p.strip()] if args.platform else None
        pending = fetch_pending(conn, limit=args.limit, platforms=platforms)
        total = len(pending)
        print(f"[fetch] {total} 건 처리 예정")
        if total == 0:
            print("처리할 항목 없음")
            return

        # product_id → brand_node_id (동기화용, brand_node_id NULL은 자연히 제외)
        id_to_brand = {r["id"]: r["brand_node_id"] for r in pending if r["brand_node_id"] is not None}
        brand_embed_counts: dict[int, int] = {}

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
            outcomes = download_batch(page, args.download_workers)
            images = persist_download_outcomes(conn, outcomes, args.dry_run)
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

            if not args.dry_run:
                for pid in embs:
                    brand_id = id_to_brand.get(pid)
                    if brand_id is not None:
                        brand_embed_counts[brand_id] = brand_embed_counts.get(brand_id, 0) + 1

            elapsed = time.time() - page_start
            done = offset + page_n
            overall_rate = done / (time.time() - start)
            eta = (total - done) / overall_rate if overall_rate > 0 else 0
            print(f"  upsert:   {n}    | page {elapsed:.1f}s | 전체 {done}/{total} | ETA {fmt_eta(eta)}")

        elapsed = time.time() - start
        print(f"\n완료 — upsert {upserted_total}/{total} (다운로드 실패 {failed_total}) · {fmt_eta(elapsed)}")

        sync_crawl_status(conn, brand_embed_counts)

        # migration 085 removed the dead monitoring view. Keep the batch's
        # end-of-run verification self-contained so a successful embedding run
        # cannot fail merely because that optional view no longer exists.
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT p.platform,
                       count(*) AS total,
                       count(pe.product_id) AS embedded,
                       round(
                           100.0 * count(pe.product_id) / nullif(count(*), 0),
                           2
                       ) AS pct_embedded
                  FROM products p
                  LEFT JOIN product_embeddings pe ON pe.product_id = p.id
                 GROUP BY p.platform
                 ORDER BY total DESC
                """
            )
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
