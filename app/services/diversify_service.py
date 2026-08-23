"""Diversify service (SPEC-ARCH-AI-001 PR1).

Cap / tolerance / order arithmetic extracted VERBATIM from the former
app/pipeline/diversify.py inline body. Behavior is byte-identical
(REQ-AI-007): the brand/platform cap loop, the break-on-target, the
banker's-rounding tolerance->target_count, and every log line are
preserved exactly. app/pipeline/diversify.py is now a thin re-export shim.
"""

import logging

from app.core.config import settings
from app.infrastructure.repositories.brand_node_cache import lookup as _brand_lookup
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

    # brandFilter 활성 시 다양성 캡을 사실상 해제한다. 사용자가 특정 브랜드를
    # 콕 집었으면(예: "글로니 상의") 그 브랜드 상품을 최대한 다 보여주는 게
    # 의도다. 단일 브랜드 결과는 brand/platform/vibe/silhouette 의 first-token 이
    # 전부 동일해서(예: GLOWNY vibe=quiet-luxury, platform=glowny) 이 캡들이
    # 겹쳐 걸리면 target 보다 훨씬 적게(관측: 5개) 잘려 나갔다. brand 필터가
    # 있을 때는 브랜드 다양성 개념이 무의미하므로 platform/vibe/silhouette 캡을
    # 끄고 brand 캡만 target 까지 허용한다.
    if req.brand_filter:
        brand_cap = max(settings.SEARCH_BRAND_CAP * 3, target)
        platform_cap = target
        vibe_cap = 0
        silhouette_cap = 0
    else:
        brand_cap = settings.SEARCH_BRAND_CAP
        platform_cap = settings.SEARCH_PLATFORM_CAP
        # SPEC-DIVERSIFY-ATTR-CAP — vibe / silhouette diversity (0 disables).
        vibe_cap = int(settings.SEARCH_VIBE_CAP or 0)
        silhouette_cap = int(settings.SEARCH_SILHOUETTE_CAP or 0)

    # 입력 / 캡 설정
    logger.info(
        "[STEP 4.7][diversify] 시작 — input=%d target=%d brand_cap=%d platform_cap=%d "
        "vibe_cap=%d silhouette_cap=%d tolerance=%.2f",
        len(state.raw_candidates),
        target,
        brand_cap,
        platform_cap,
        vibe_cap,
        silhouette_cap,
        req.tolerance,
    )

    seen_brand: dict[str, int] = {}
    seen_platform: dict[str, int] = {}
    # SPEC-DIVERSIFY-ATTR-CAP — first-token counters. brand_node_cache miss →
    # candidate bypasses these caps (fail-open: never drop a candidate we
    # can't classify). Empty list / no first token → also bypass.
    seen_vibe: dict[str, int] = {}
    seen_silhouette: dict[str, int] = {}
    # @MX:NOTE: [AUTO] SPEC-AGENT-UX-P0-001 REQ-UX-001 — product_id 레벨 dedup.
    # v6 RPC distance-tie 또는 refine cumulative merge 에서 동일 id 가 두 번
    # 들어와도 사용자에게 한 번만 노출. falsy id 는 bypass (graceful fallback).
    seen_ids: set[str] = set()
    # 260522: content-level dedup. The catalog has the SAME product scraped under
    # different product_ids (live: 'Rier t-shirt, fog ₩1,295,000' appeared as #4
    # AND #5 — distinct ids, identical brand+name+price). The id-only guard let
    # both through (drops_dup=0). Key on (brand, name, price) so visual dupes are
    # collapsed regardless of id. Falsy/empty content → no content key (bypass,
    # graceful — never drop an item we can't identify).
    seen_content: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    drops_brand = 0
    drops_platform = 0
    drops_dup = 0
    drops_vibe = 0
    drops_silhouette = 0

    for c in state.raw_candidates:
        pid = c.get("id")
        if pid and pid in seen_ids:
            drops_dup += 1
            continue
        brand = (c.get("brand") or "").lower()
        # Content key: brand + normalized name + price. Only used when both
        # brand and name are present (else it cannot reliably identify a dupe).
        name_norm = " ".join(str(c.get("name") or "").lower().split())
        price_key = str(c.get("price") or "")
        content_key = (brand, name_norm, price_key) if (brand and name_norm) else None
        if content_key is not None and content_key in seen_content:
            drops_dup += 1
            continue
        platform = (c.get("platform") or "").lower()
        if seen_brand.get(brand, 0) >= brand_cap:
            drops_brand += 1
            continue
        if seen_platform.get(platform, 0) >= platform_cap:
            drops_platform += 1
            continue
        # SPEC-DIVERSIFY-ATTR-CAP — vibe / silhouette first-token caps. Lookup
        # via brand_node_cache; miss → bypass (the candidate cannot be
        # classified, never drop). Cap == 0 also bypasses (kill-switch).
        attrs = _brand_lookup(c.get("brand"))
        vibe_key = attrs.vibe[0] if (attrs and attrs.vibe) else ""
        silhouette_key = attrs.silhouette[0] if (attrs and attrs.silhouette) else ""
        if vibe_cap > 0 and vibe_key and seen_vibe.get(vibe_key, 0) >= vibe_cap:
            drops_vibe += 1
            continue
        if silhouette_cap > 0 and silhouette_key and seen_silhouette.get(silhouette_key, 0) >= silhouette_cap:
            drops_silhouette += 1
            continue
        out.append(c)
        if pid:
            seen_ids.add(pid)
        if content_key is not None:
            seen_content.add(content_key)
        seen_brand[brand] = seen_brand.get(brand, 0) + 1
        seen_platform[platform] = seen_platform.get(platform, 0) + 1
        if vibe_key:
            seen_vibe[vibe_key] = seen_vibe.get(vibe_key, 0) + 1
        if silhouette_key:
            seen_silhouette[silhouette_key] = seen_silhouette.get(silhouette_key, 0) + 1
        if len(out) >= target:
            break

    # 출력 / 캡 통계 / 브랜드·플랫폼·vibe·silhouette 분포
    brand_dist = sorted(seen_brand.items(), key=lambda x: -x[1])[:5]
    platform_dist = sorted(seen_platform.items(), key=lambda x: -x[1])[:5]
    vibe_dist = sorted(seen_vibe.items(), key=lambda x: -x[1])[:5]
    silhouette_dist = sorted(seen_silhouette.items(), key=lambda x: -x[1])[:5]
    logger.info(
        "[STEP 4.8][diversify] 끝 — out=%d drops_brand=%d drops_platform=%d drops_dup=%d "
        "drops_vibe=%d drops_silhouette=%d",
        len(out),
        drops_brand,
        drops_platform,
        drops_dup,
        drops_vibe,
        drops_silhouette,
    )
    logger.info("[STEP 4.8][diversify] brand_top5=%s", brand_dist)
    logger.info("[STEP 4.8][diversify] platform_top5=%s", platform_dist)
    logger.info("[STEP 4.8][diversify] vibe_top5=%s silhouette_top5=%s", vibe_dist, silhouette_dist)

    state.final_candidates = out
    state.counts["after_diversify"] = len(out)
    state.counts["final"] = len(out)
    state.end("diversify")
    return state
