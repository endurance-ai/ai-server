import io

import httpx
from PIL import Image

from scripts.embed_batch_devapp import (
    DownloadOutcome,
    ImageRepair,
    SnapshotImage,
    classify_download_error,
    download_product_image,
    extract_shopify_images,
    fetch_pending,
    persist_download_outcomes,
    upsert,
)


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(output, format="JPEG")
    return output.getvalue()


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, params: list[object]) -> None:
        self.conn.calls.append((query, params))

    def fetchone(self):
        return self.conn.responses.pop(0)

    def fetchall(self):
        return self.conn.rows


class FakeConnection:
    def __init__(self, responses: list[object], rows: list[dict] | None = None) -> None:
        self.responses = responses
        self.rows = rows or []
        self.calls: list[tuple[str, list[object]]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **_kwargs):
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_http_404_is_permanent_but_503_is_retryable() -> None:
    request = httpx.Request("GET", "https://cdn.example/broken.jpg")
    missing = httpx.HTTPStatusError(
        "missing",
        request=request,
        response=httpx.Response(404, request=request),
    )
    unavailable = httpx.HTTPStatusError(
        "unavailable",
        request=request,
        response=httpx.Response(503, request=request),
    )

    assert classify_download_error(str(request.url), missing).disposition == "permanent"
    assert classify_download_error(str(request.url), unavailable).disposition == "retryable"


def test_extract_shopify_images_covers_native_image_shapes() -> None:
    assert extract_shopify_images(
        {
            "images": ["https://cdn.example/front.jpg", {"src": "https://cdn.example/back.jpg"}],
            "featured_image": {"url": "https://cdn.example/hero.jpg"},
            "media": [{"preview_image": {"src": "https://cdn.example/detail.jpg"}}],
            "variants": [{"featured_image": {"src": "https://cdn.example/blue.jpg"}}],
        }
    ) == [
        "https://cdn.example/front.jpg",
        "https://cdn.example/back.jpg",
        "https://cdn.example/hero.jpg",
        "https://cdn.example/detail.jpg",
        "https://cdn.example/blue.jpg",
    ]


def test_broken_canonical_promotes_verified_gallery_image() -> None:
    canonical = "https://cdn.shopify.com/files/broken.jpg"
    replacement = "https://cdn.shopify.com/files/good.jpg"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == canonical:
            return httpx.Response(404, request=request)
        if str(request.url) == replacement:
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=image_bytes(),
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    row = {
        "id": "42",
        "image_url": canonical,
        "image_revision": "7",
        "source_image_url": canonical,
        "images": [canonical, replacement],
        "product_url": "https://shop.example/products/coat",
        "image_failure_attempts": 0,
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome = download_product_image(client, row)

    assert outcome.image is not None
    assert outcome.failure is None
    assert outcome.repair is not None
    assert outcome.repair.replacement_url == replacement
    assert outcome.repair.images[0] == replacement
    assert outcome.repair.bad_urls == [canonical]
    assert outcome.canonical_revision == "7"


def test_missing_shopify_product_clears_image_and_marks_out_of_stock() -> None:
    canonical = "https://cdn.shopify.com/files/gone.jpg"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    row = {
        "id": "43",
        "image_url": canonical,
        "image_revision": "9",
        "source_image_url": canonical,
        "images": [canonical],
        "product_url": "https://shop.example/products/gone",
        "image_failure_attempts": 0,
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome = download_product_image(client, row)

    assert outcome.image is None
    assert outcome.failure is not None
    assert outcome.failure.disposition == "permanent"
    assert outcome.repair is not None
    assert outcome.repair.replacement_url is None
    assert outcome.repair.images == []
    assert outcome.repair.mark_out_of_stock is True


def test_retryable_canonical_failure_does_not_mutate_image_fields() -> None:
    canonical = "https://cdn.example/slow.jpg"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    row = {
        "id": "44",
        "image_url": canonical,
        "image_revision": "11",
        "source_image_url": canonical,
        "images": [canonical, "https://cdn.example/alternate.jpg"],
        "product_url": "https://shop.example/products/slow",
        "image_failure_attempts": 0,
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        outcome = download_product_image(client, row)

    assert outcome.image is None
    assert outcome.repair is None
    assert outcome.failure is not None
    assert outcome.failure.disposition == "retryable"


def test_repaired_image_uses_revision_returned_by_v2_rpc() -> None:
    canonical = "https://cdn.example/broken.jpg"
    replacement = "https://cdn.example/repaired.jpg"
    conn = FakeConnection(
        [
            (
                [
                    {
                        "id": "42",
                        "outcome": "applied",
                        "image_url": replacement,
                        "image_revision": "8",
                    }
                ],
            )
        ]
    )
    outcome = DownloadOutcome(
        product_id="42",
        canonical_url=canonical,
        canonical_revision="7",
        image=Image.new("RGB", (1, 1)),
        repair=ImageRepair("42", canonical, replacement, replacement, [replacement], [canonical], False),
    )

    images = persist_download_outcomes(conn, [outcome], dry_run=False)

    assert images["42"].source_image_url == replacement
    assert images["42"].source_image_revision == "8"
    query, params = conn.calls[0]
    assert query == "SELECT repair_product_image_assets_v2(%s)"
    assert params[0].obj[0]["before_revision"] == "7"


def test_devapp_writer_uses_v2_contract_and_only_tracks_applied_ids() -> None:
    conn = FakeConnection([([{"id": "1", "outcome": "applied"}],)])
    images = {
        "1": SnapshotImage(Image.new("RGB", (1, 1)), "https://img/1", "3"),
        "2": SnapshotImage(Image.new("RGB", (1, 1)), "https://img/2", "5"),
    }

    result = upsert(conn, {"1": [1.0], "2": [1.0]}, images, dry_run=False, chunk_size=25)

    assert (result.applied, result.stale, result.missing, result.failed) == (1, 0, 0, 1)
    assert result.applied_ids == {"1"}
    query, params = conn.calls[0]
    assert query == "SELECT bulk_update_product_embeddings_v2(%s)"
    assert params[0].obj[0]["source_image_url"] == "https://img/1"
    assert params[0].obj[1]["source_image_revision"] == "5"


def test_devapp_dry_run_does_not_execute_writer() -> None:
    conn = FakeConnection([])
    images = {"1": SnapshotImage(Image.new("RGB", (1, 1)), "https://img/1", "3")}

    result = upsert(conn, {"1": [1.0]}, images, dry_run=True, chunk_size=25)

    assert result.planned == 1
    assert result.applied == 0
    assert conn.calls == []


def test_fetch_pending_preserves_platform_filter_and_reads_image_revision() -> None:
    conn = FakeConnection([], rows=[{"id": 9007199254740993, "image_revision": 17}])

    rows = fetch_pending(conn, limit=2, platforms=["samostuff", "teak"])

    assert rows[0]["id"] == "9007199254740993"
    query, params = conn.calls[0]
    assert "p.image_revision" in query
    assert "p.platform = ANY(%s)" in query
    assert "LIMIT %s" in query
    assert params == [["samostuff", "teak"], 2]
