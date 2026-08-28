import pytest

from app.services import editorial_candidates
from app.services.editorial_candidates import (
    EditorialQuery,
    _parse_review_scores,
    _prepare_recall_pool,
    _review_candidates,
    _sanitize_plan,
    _select_final_candidates,
)


def _row(
    product_id: int,
    *,
    brand: str,
    image: str | None = None,
    distance: float = 0.1,
    price: int = 100_000,
) -> dict:
    return {
        "id": product_id,
        "brand": brand,
        "name": f"Product {product_id}",
        "price": price,
        "image_url": image or f"https://cdn.example.com/{product_id}.jpg",
        "product_url": f"https://shop.example.com/{product_id}",
        "platform": "test",
        "distance": distance,
        "degraded": False,
    }


def test_sanitize_plan_requires_unique_english_visual_queries() -> None:
    summary, queries = _sanitize_plan(
        {
            "summary": "여름 휴양지 무드",
            "queries": [
                {"label": "탑", "query": " fitted lace crop top ", "category": "top"},
                {"label": "중복", "query": "fitted lace crop top", "category": "top"},
                {"label": "한글", "query": "레이스 탑", "category": "top"},
                {"label": "스커트", "query": "low rise denim mini skirt", "category": "skirt"},
            ],
        }
    )

    assert summary == "여름 휴양지 무드"
    assert [query.query for query in queries] == [
        "fitted lace crop top",
        "low rise denim mini skirt",
    ]


def test_prepare_recall_pool_interleaves_queries_and_rejects_bad_catalog_rows() -> None:
    top = EditorialQuery(label="탑", query="lace crop top", category="top")
    dress = EditorialQuery(label="드레스", query="bodycon mini dress", category="dress")
    duplicate_image = "https://cdn.example.com/shared.jpg"

    rows = _prepare_recall_pool(
        [
            (
                top,
                [
                    _row(1, brand="A", image=duplicate_image),
                    _row(2, brand="A", image="https://cdn.example.com/animated.gif"),
                    _row(3, brand="상품명 : broken field"),
                ],
            ),
            (
                dress,
                [
                    _row(4, brand="B"),
                    _row(5, brand="C", image=duplicate_image),
                    _row(6, brand="D", price=0),
                ],
            ),
        ]
    )

    assert [row["id"] for row in rows] == [1, 4]
    assert rows[0]["matched_query"] == "lace crop top"
    assert rows[1]["query_label"] == "드레스"


def test_parse_review_scores_clamps_values_and_ignores_malformed_rows() -> None:
    scores = _parse_review_scores(
        {
            "scores": [
                {
                    "product_id": 1,
                    "concept_score": 140,
                    "image_quality_score": -5,
                    "reason": "검수",
                },
                {"product_id": "bad", "concept_score": 80, "image_quality_score": 80},
            ]
        }
    )

    assert scores == {
        1: {
            "concept_score": 100,
            "image_quality_score": 0,
            "reason": "검수",
        }
    }


def test_select_final_candidates_uses_brand_cap_as_soft_diversity_target() -> None:
    recall = []
    for product_id, brand, query in [
        (1, "A", "crop top"),
        (2, "A", "mini skirt"),
        (3, "A", "mini dress"),
        (4, "A", "shoulder bag"),
        (5, "B", "crop top"),
        (6, "C", "mini skirt"),
        (7, "D", "mini dress"),
    ]:
        row = _row(product_id, brand=brand, distance=product_id / 100)
        row.update({"matched_query": query, "query_label": query})
        recall.append(row)

    scores = {
        product_id: {
            "concept_score": 85,
            "image_quality_score": 80,
            "reason": "적합",
        }
        for product_id in range(1, 8)
    }
    scores[7]["concept_score"] = 40

    selected = _select_final_candidates(recall, scores, limit=10, brand_cap=3)

    assert [candidate.id for candidate in selected] == [1, 2, 3, 4, 5, 6]
    assert all(candidate.editorial_score == 83.5 for candidate in selected)
    assert all(candidate.id != 7 for candidate in selected)


@pytest.mark.asyncio
async def test_review_candidates_retries_only_missing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[int]] = []

    async def fake_review_batch(_concept: str, candidates: list[dict]) -> dict:
        ids = [int(candidate["id"]) for candidate in candidates]
        calls.append(ids)
        if len(calls) == 1:
            ids = ids[:-2]
        return {
            product_id: {
                "concept_score": 80,
                "image_quality_score": 80,
                "reason": "clear concept fit",
            }
            for product_id in ids
        }

    monkeypatch.setattr(editorial_candidates, "_review_batch", fake_review_batch)
    candidates = [_row(product_id, brand=f"B{product_id}") for product_id in range(1, 9)]

    scores = await _review_candidates("test concept", candidates)

    assert sorted(scores) == list(range(1, 9))
    assert calls == [list(range(1, 9)), [7, 8]]


def test_select_final_candidates_rejects_generic_sixty_point_match() -> None:
    row = _row(1, brand="A")
    row.update({"matched_query": "mini dress", "query_label": "드레스"})

    selected = _select_final_candidates(
        [row],
        {
            1: {
                "concept_score": 65,
                "image_quality_score": 95,
                "reason": "clean but generic",
            }
        },
        limit=10,
    )

    assert selected == []


def test_select_final_candidates_does_not_promote_weak_axis_to_the_top() -> None:
    recall = []
    scores = {}
    for product_id, query, concept_score in [
        (1, "top", 95),
        (2, "dress", 90),
        (3, "shoes", 70),
    ]:
        row = _row(product_id, brand=f"B{product_id}")
        row.update({"matched_query": query, "query_label": query})
        recall.append(row)
        scores[product_id] = {
            "concept_score": concept_score,
            "image_quality_score": 100,
            "reason": "reviewed",
        }

    selected = _select_final_candidates(recall, scores, limit=10)

    assert [candidate.id for candidate in selected] == [1, 2, 3]
