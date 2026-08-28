import io

import httpx
from PIL import Image

from scripts.embed_batch_devapp import (
    classify_download_error,
    download_product_image,
    extract_shopify_images,
)


def image_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(output, format="JPEG")
    return output.getvalue()


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


def test_missing_shopify_product_clears_image_and_marks_out_of_stock() -> None:
    canonical = "https://cdn.shopify.com/files/gone.jpg"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request)

    row = {
        "id": "43",
        "image_url": canonical,
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
