"""Diversify service (SPEC-ARCH-AI-001 PR1).

Cap / tolerance / order arithmetic extracted VERBATIM from the former
app/pipeline/diversify.py inline body. Behavior is byte-identical
(REQ-AI-007): the brand/platform cap loop, the break-on-target, the
banker's-rounding tolerance->target_count, and every log line are
preserved exactly. app/pipeline/diversify.py is now a thin re-export shim.
"""

import logging

from app.core.config import settings
from app.pipeline.state import PipelineState

logger = logging.getLogger(__name__)


def tolerance_to_target_count(tolerance: float) -> int:
    """v4 toleranceToTargetCount 포팅 (locked-filter.ts).
    0.0 → tight (10), 0.5 → medium (15), 1.0 → loose (20).
    """
    t = max(0.0, min(1.0, tolerance))
    return int(round(10 + t * 10))


async def diversify_service(state: PipelineState) -> PipelineState:
    state.start("diversify")
    req = state.request
    target = req.final_limit or tolerance_to_target_count(req.tolerance)

    # brandFilter 활성 시 브랜드 캡 완화 (v4와 동일 정책)
    brand_cap = settings.SEARCH_BRAND_CAP * 3 if req.brand_filter else settings.SEARCH_BRAND_CAP
    platform_cap = settings.SEARCH_PLATFORM_CAP

    # 입력 / 캡 설정
    logger.info(
        "[STEP 4.7][diversify] 시작 — input=%d target=%d brand_cap=%d platform_cap=%d tolerance=%.2f",
        len(state.raw_candidates),
        target,
        brand_cap,
        platform_cap,
        req.tolerance,
    )

    seen_brand: dict[str, int] = {}
    seen_platform: dict[str, int] = {}
    out: list[dict] = []
    drops_brand = 0
    drops_platform = 0

    for c in state.raw_candidates:
        brand = (c.get("brand") or "").lower()
        platform = (c.get("platform") or "").lower()
        if seen_brand.get(brand, 0) >= brand_cap:
            drops_brand += 1
            continue
        if seen_platform.get(platform, 0) >= platform_cap:
            drops_platform += 1
            continue
        out.append(c)
        seen_brand[brand] = seen_brand.get(brand, 0) + 1
        seen_platform[platform] = seen_platform.get(platform, 0) + 1
        if len(out) >= target:
            break

    # 출력 / 캡 통계 / 브랜드·플랫폼 분포
    brand_dist = sorted(seen_brand.items(), key=lambda x: -x[1])[:5]
    platform_dist = sorted(seen_platform.items(), key=lambda x: -x[1])[:5]
    logger.info(
        "[STEP 4.8][diversify] 끝 — out=%d drops_brand=%d drops_platform=%d",
        len(out),
        drops_brand,
        drops_platform,
    )
    logger.info("[STEP 4.8][diversify] brand_top5=%s", brand_dist)
    logger.info("[STEP 4.8][diversify] platform_top5=%s", platform_dist)

    state.final_candidates = out
    state.counts["after_diversify"] = len(out)
    state.counts["final"] = len(out)
    state.end("diversify")
    return state
