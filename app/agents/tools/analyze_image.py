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


def _is_blocked_host(host: str) -> bool:
    """Textual bare-IP-literal / loopback / RFC-1918 / link-local match.

    No DNS resolution — matches REQ-AGENT-SEC-URL-001's enumerated set.
    """
    if host in {"localhost", "::1"}:
        return True
    # IPv6 loopback may arrive bracket-stripped by urlparse already.
    if host.startswith("127."):  # 127.0.0.0/8 loopback
        return True
    if host.startswith("10."):  # 10.0.0.0/8 RFC-1918
        return True
    if host.startswith("192.168."):  # 192.168.0.0/16 RFC-1918
        return True
    if host.startswith("169.254."):  # link-local / cloud metadata (169.254.169.254)
        return True
    # 172.16.0.0/12 — 172.16.x – 172.31.x
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2 and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return True
    return False


def _ssrf_ok(url: str) -> tuple[bool, str | None]:
    # P1-4: UNCONDITIONAL hard-deny — fires regardless of allowlist contents
    # and BEFORE the allowlist check (REQ-AGENT-SEC-URL-001). Closes the
    # open-by-default hole when ALLOWED_IMAGE_HOSTS is empty.
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False, "scheme_not_http"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "no_host"
    if _is_blocked_host(host):
        return False, f"private_or_loopback:{host}"

    # Additional narrowing layer — allowlist (when configured).
    allowed = settings.allowed_image_hosts
    if not allowed:
        return True, None
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
