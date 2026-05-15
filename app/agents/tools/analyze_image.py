"""SPEC-AGENT-V2-REACT / T-003a — `analyze_image` tool wrapper.

Thin wrapper around `app.channels.vision.extract`. Adds SSRF guard via the
same `settings.allowed_image_hosts` cascade used by `RecommendRequest`.

@MX:NOTE: [AUTO] Side effect: calls Vision LLM (LiteLLM proxy).
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from app.agents.tool_registry import AnalyzeImageResult
from app.core.config import settings

logger = logging.getLogger(__name__)


def _ssrf_ok(url: str) -> tuple[bool, str | None]:
    allowed = settings.allowed_image_hosts
    if not allowed:
        return True, None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "scheme_not_http"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "no_host"
    if not any(host == a or host.endswith(f".{a}") for a in allowed):
        return False, f"host_not_allowed:{host}"
    return True, None


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> AnalyzeImageResult:
    image_url = (args.get("image_url") or "").strip()
    if not image_url:
        return AnalyzeImageResult(ok=False, error="missing_image_url")

    ssrf_ok, ssrf_err = _ssrf_ok(image_url)
    if not ssrf_ok:
        return AnalyzeImageResult(ok=False, error=f"ssrf_blocked:{ssrf_err}")

    try:
        from app.channels.vision import extract as vision_extract

        result = await vision_extract(image_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tool.analyze_image] vision raised: %r", exc)
        return AnalyzeImageResult(ok=False, error=f"vision_failed:{type(exc).__name__}")

    style_primary: str | None = None
    try:
        sn = getattr(result, "styleNode", None)
        if sn is not None:
            style_primary = getattr(sn, "primary", None)
    except Exception:  # noqa: BLE001
        pass

    items = list(getattr(result, "items", None) or [])
    first = items[0] if items else None

    return AnalyzeImageResult(
        ok=True,
        error=None,
        style_node_primary=style_primary,
        mood=list(getattr(result, "mood", None) or [])[:5],
        palette=list(getattr(result, "palette", None) or [])[:5],
        items_count=len(items),
        subcategory=getattr(first, "subcategory", None) if first else None,
        fit=getattr(first, "fit", None) if first else None,
        color_family=getattr(first, "color_family", None) if first else None,
        search_query=getattr(first, "search_query", None) if first else None,
    )
