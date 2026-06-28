from httpx import AsyncClient


async def test_list_style_nodes_returns_loaded_taxonomy(client: AsyncClient, monkeypatch):
    from app.infrastructure.repositories import style_node

    monkeypatch.setattr(
        style_node,
        "list_nodes",
        lambda: [
            style_node.StyleNodeSnapshot(
                id=3,
                code="C",
                name_en="Minimal",
                keywords_en=("clean", "quiet"),
            )
        ],
    )
    monkeypatch.setattr(style_node, "is_warmed", lambda: True)

    resp = await client.get("/v1/style-nodes")

    assert resp.status_code == 200
    assert resp.json() == {
        "warmed": True,
        "nodes": [
            {
                "id": 3,
                "code": "C",
                "name_en": "Minimal",
                "keywords_en": ["clean", "quiet"],
            }
        ],
    }


async def test_list_style_nodes_fallback_shape(client: AsyncClient):
    resp = await client.get("/v1/style-nodes")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["warmed"], bool)
    assert len(data["nodes"]) >= 21
    assert data["nodes"][0] == {
        "id": 1,
        "code": "A",
        "name_en": None,
        "keywords_en": [],
    }
