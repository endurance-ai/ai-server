from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal

from app.models.request import RecommendRequest

EnhanceQueryStatus = Literal["ok", "fallback", "disabled", "skipped"]


@dataclass
class PipelineState:
    """파이프라인이 단계별로 갱신해 나가는 가변 상태.
    모든 step 함수는 (state) -> state 시그니처로 통일 — 향후 LangGraph 마이그레이션 비용 0.
    """

    request: RecommendRequest

    # SPEC-PERSONALIZE-RERANK — caller (search_products tool / refine_search)
    # injects the per-turn user_key so search_service can look up TasteProfile
    # and re-order raw_candidates before diversify. None on the public
    # /recommend path (no per-user context) → rerank is a no-op.
    user_key: str | None = None

    # SPEC-SEARCH-HYBRID-001 — 순수 텍스트 쿼리 여부. True 면 search_service 가
    # 이미지⊕텍스트 임베딩 블렌드 RPC(search_products_hybrid_v1)로 랭킹한다.
    # 사진/blended/pinned-anchor 경로는 query_embedding 이 이미지 벡터라 False
    # (v6 이미지 단독 랭킹 유지 — text_embedding 블렌드는 cross-modal 미검증).
    hybrid_text: bool = False

    # 중간 산출물
    embedding: list[float] | None = None
    raw_candidates: list[dict[str, Any]] = field(default_factory=list)
    final_candidates: list[dict[str, Any]] = field(default_factory=list)

    # enhance_query (SPEC-PIPELINE-001) — status=="ok" 일 때만 enhanced_* 사용
    enhanced_query: str | None = None
    enhanced_query_ko: str | None = None
    enhance_query_status: EnhanceQueryStatus = "disabled"

    # 측정
    counts: dict[str, int] = field(default_factory=dict)
    latency_ms: dict[str, int] = field(default_factory=dict)
    _step_starts: dict[str, float] = field(default_factory=dict)

    def start(self, step: str) -> None:
        self._step_starts[step] = perf_counter()

    def end(self, step: str) -> None:
        start = self._step_starts.get(step)
        if start is None:
            return
        self.latency_ms[step] = int((perf_counter() - start) * 1000)
