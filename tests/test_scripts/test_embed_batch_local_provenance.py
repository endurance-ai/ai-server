"""Offline characterization of the local embedding writer's v2 boundary."""

from PIL import Image

from scripts.embed_batch_local import SnapshotImage, upsert


class FakeRpcResponse:
    def __init__(self, data: object) -> None:
        self.data = data


class FakeRpcClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, name: str, params: dict):
        self.calls.append((name, params))
        return self

    def execute(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeRpcResponse(response)


def test_local_runner_preserves_source_and_counts_unreported_rows_as_failed() -> None:
    client = FakeRpcClient([[{"id": "1", "outcome": "applied"}]])
    images = {
        "1": SnapshotImage(Image.new("RGB", (1, 1)), "https://img/a", "7"),
        "2": SnapshotImage(Image.new("RGB", (1, 1)), "https://img/b", "9"),
    }

    result = upsert(client, {"1": [1.0], "2": [1.0]}, images, chunk_size=25)

    assert (result.applied, result.stale, result.missing, result.failed) == (1, 0, 0, 1)
    name, params = client.calls[0]
    assert name == "bulk_update_product_embeddings_v2"
    assert params["payload"][0]["source_image_revision"] == "7"
    assert params["payload"][1]["source_image_url"] == "https://img/b"


def test_stale_and_deleted_products_are_not_counted_as_applied() -> None:
    client = FakeRpcClient([[{"id": "1", "outcome": "stale"}, {"id": "2", "outcome": "missing"}]])
    images = {
        "1": SnapshotImage(Image.new("RGB", (1, 1)), "https://img/a", "1"),
        "2": SnapshotImage(Image.new("RGB", (1, 1)), "https://img/b", "3"),
    }

    result = upsert(client, {"1": [1.0], "2": [1.0]}, images)

    assert (result.applied, result.stale, result.missing, result.failed) == (0, 1, 1, 0)


def test_timeout_retry_keeps_the_original_source_snapshot() -> None:
    client = FakeRpcClient([RuntimeError("57014 statement timeout"), [{"id": "1", "outcome": "applied"}]])
    images = {"1": SnapshotImage(Image.new("RGB", (1, 1)), "https://img/a", "9007199254740993")}

    result = upsert(client, {"1": [1.0]}, images, chunk_size=25)

    assert result.applied == 1
    assert len(client.calls) == 2
    assert client.calls[0][1]["payload"] == client.calls[1][1]["payload"]
    assert client.calls[1][1]["payload"][0]["source_image_revision"] == "9007199254740993"
