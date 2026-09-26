import json
import sys
from unittest.mock import Mock

import httpx
import pytest

from scripts import embed_batch_devapp as batch
from tests.test_scripts.test_embed_batch_devapp import FakeConnection, image_bytes


@pytest.mark.parametrize(
    "values", [[], {}, [1], [True], [None], [""], ["0"], ["-1"], ["1.0"], [" 1"], ["01"], ["9223372036854775808"]]
)
def test_id_manifest_rejects_ambiguous_or_unbounded_scope(tmp_path, values):
    manifest = tmp_path / "ids.json"
    manifest.write_text(json.dumps(values))
    with pytest.raises(ValueError):
        batch.load_product_ids(manifest)


def test_id_manifest_preserves_bigint_precision_and_deduplicates(tmp_path):
    manifest = tmp_path / "ids.json"
    manifest.write_text('["9223372036854775807", "42", "42"]')
    assert batch.load_product_ids(manifest) == ["9223372036854775807", "42"]


def test_explicit_empty_ids_never_fall_back_to_global_query():
    conn = FakeConnection([])
    assert batch.fetch_pending(conn, limit=None, product_ids=[]) == []
    query, params = conn.calls[0]
    assert "p.id = ANY(%s::bigint[])" in query
    assert params == [[], batch.PAGE_SIZE]


def test_scope_intersects_platforms_and_ids_and_includes_out_of_stock():
    conn = FakeConnection([])
    batch.fetch_pending(conn, limit=2, platforms=["zara", "suade"], product_ids=["42"])
    query, params = conn.calls[0]
    assert "p.platform = ANY(%s)" in query
    assert "p.id = ANY(%s::bigint[])" in query
    assert "in_stock" not in query
    assert "pif.disposition = 'retryable'" in query
    assert "NOT EXISTS" in query
    assert params == [["zara", "suade"], ["42"], 2]


def test_pending_pagination_keeps_scope_and_snapshot_revision(monkeypatch):
    monkeypatch.setattr(batch, "PAGE_SIZE", 2)
    pages = iter(
        [
            [{"id": 9007199254740993, "image_revision": 7}, {"id": 9007199254740994, "image_revision": 8}],
            [{"id": 9007199254740995, "image_revision": 9}],
        ]
    )
    conn = FakeConnection([])
    cursor = conn.cursor()
    cursor.fetchall = lambda: next(pages)
    monkeypatch.setattr(conn, "cursor", lambda **kwargs: cursor)
    rows = batch.fetch_pending(conn, limit=3, platforms=["suade"])
    assert [row["id"] for row in rows] == ["9007199254740993", "9007199254740994", "9007199254740995"]
    assert [row["image_revision"] for row in rows] == ["7", "8", "9"]
    assert conn.calls[0][1] == [["suade"], 2]
    assert "p.id > %s" in conn.calls[1][0]
    assert conn.calls[1][1] == [["suade"], "9007199254740994", 1]


@pytest.mark.parametrize(
    "argv",
    [
        ["--ids-file", ""],
        ["--ids-file", "/nonexistent/ids.json"],
        ["--platform", ""],
        ["--platform", ", ,"],
        ["--limit", "0"],
        ["--batch-size", "0"],
        ["--download-workers", "-1"],
        ["--upsert-chunk", "0"],
    ],
)
def test_invalid_cli_scope_fails_before_database_or_model(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["embed_batch_devapp.py", *argv])
    connect = Mock(side_effect=AssertionError("must not connect"))
    model = Mock(side_effect=AssertionError("must not load model"))
    monkeypatch.setattr(batch.psycopg, "connect", connect)
    monkeypatch.setattr(batch, "load_model", model)
    with pytest.raises(SystemExit) as error:
        batch.main()
    assert error.value.code == 2
    connect.assert_not_called()
    model.assert_not_called()


def test_no_pending_products_does_not_load_model(monkeypatch, tmp_path):
    manifest = tmp_path / "ids.json"
    manifest.write_text('["42"]')
    monkeypatch.setattr(
        sys, "argv", ["embed_batch_devapp.py", "--ids-file", str(manifest), "--platform", "suade, zara"]
    )
    monkeypatch.setenv("KIKOAI_DEVAPP_DSN", "postgresql://unused")
    conn = Mock()
    fetch = Mock(return_value=[])
    model = Mock(side_effect=AssertionError("must not load model"))
    monkeypatch.setattr(batch.psycopg, "connect", Mock(return_value=conn))
    monkeypatch.setattr(batch, "fetch_pending", fetch)
    monkeypatch.setattr(batch, "load_model", model)
    assert batch.main() == 0
    fetch.assert_called_once_with(conn, limit=None, platforms=["suade", "zara"], product_ids=["42"])
    model.assert_not_called()
    conn.close.assert_called_once()


def test_coverage_uses_the_same_scope():
    conn = FakeConnection([])
    assert batch.fetch_coverage(conn, platforms=["zara"], product_ids=[]) == []
    query, params = conn.calls[0]
    assert "p.platform = ANY(%s)" in query
    assert "p.id = ANY(%s::bigint[])" in query
    assert params == [["zara"], []]


def test_zara_retries_transient_denials_with_its_own_referer(monkeypatch):
    requests = []
    sleep = Mock()
    monkeypatch.setattr(batch.time, "sleep", sleep)

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(403 if len(requests) == 1 else 429)
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=image_bytes())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert batch.download_image(client, "https://static.zara.net/photo.jpg").size == (4, 4)
    assert len(requests) == 3
    assert all(request.headers["referer"] == "https://www.zara.com/" for request in requests)
    assert all("Mozilla/5.0" in request.headers["user-agent"] for request in requests)
    assert sleep.call_count == 2


@pytest.mark.parametrize(
    "url,status,attempts",
    [
        ("https://static.zara.net/photo.jpg", 403, 3),
        ("https://static.zara.net/photo.jpg", 404, 1),
        ("https://static.zara.net/photo.jpg", 503, 1),
        ("https://static.zara.net.evil.example/photo.jpg", 403, 1),
        ("https://cdn.example/photo.jpg", 429, 1),
    ],
)
def test_image_retry_is_bounded_and_zara_only(monkeypatch, url, status, attempts):
    requests = []
    monkeypatch.setattr(batch.time, "sleep", Mock())

    def handler(request):
        requests.append(request)
        return httpx.Response(status)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            batch.download_image(client, url)
    assert len(requests) == attempts
    if "evil.example" in url or "cdn.example" in url:
        assert all("referer" not in request.headers for request in requests)
