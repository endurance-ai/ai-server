from app.core.config import settings
from app.observability.langfuse import observe
from app.pipeline.state import PipelineState


def _tolerance_to_target_count(tolerance: float) -> int:
    """v4 toleranceToTargetCount 포팅 (locked-filter.ts).
    0.0 → tight (10), 0.5 → medium (15), 1.0 → loose (20).
    """
    t = max(0.0, min(1.0, tolerance))
    return int(round(10 + t * 10))


@observe(name="pipeline.diversify")
async def diversify_step(state: PipelineState) -> PipelineState:
    state.start("diversify")
    req = state.request
    target = req.final_limit or _tolerance_to_target_count(req.tolerance)

    # brandFilter 활성 시 브랜드 캡 완화 (v4와 동일 정책)
    brand_cap = settings.SEARCH_BRAND_CAP * 3 if req.brand_filter else settings.SEARCH_BRAND_CAP
    platform_cap = settings.SEARCH_PLATFORM_CAP

    seen_brand: dict[str, int] = {}
    seen_platform: dict[str, int] = {}
    out: list[dict] = []

    for c in state.raw_candidates:
        brand = (c.get("brand") or "").lower()
        platform = (c.get("platform") or "").lower()
        if seen_brand.get(brand, 0) >= brand_cap:
            continue
        if seen_platform.get(platform, 0) >= platform_cap:
            continue
        out.append(c)
        seen_brand[brand] = seen_brand.get(brand, 0) + 1
        seen_platform[platform] = seen_platform.get(platform, 0) + 1
        if len(out) >= target:
            break

    state.final_candidates = out
    state.counts["after_diversify"] = len(out)
    state.counts["final"] = len(out)
    state.end("diversify")
    return state
