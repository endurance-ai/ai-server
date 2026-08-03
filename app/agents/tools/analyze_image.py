"""SPEC-AGENT-V2-REACT / T-003a — `analyze_image` tool wrapper.

Thin wrapper around `app.channels.vision.extract`. Adds SSRF guard via the
same `settings.allowed_image_hosts` cascade used by `RecommendRequest`.

@MX:NOTE: [AUTO] Side effect: calls Vision LLM (LiteLLM proxy).
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any
from urllib.parse import urlparse

from app.agents.tool_registry import AnalyzeImageResult
from app.core.config import settings

logger = logging.getLogger(__name__)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Block any canonicalized loopback / private / link-local / RFC-1918 IP.

    Also unwraps IPv4-mapped/-compatible IPv6 (e.g. ::ffff:127.0.0.1) so an
    embedded loopback is caught after canonicalization.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_unspecified


def _canonical_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Best-effort canonicalization of an IP literal in any common encoding.

    Handles dotted IPv4, IPv6 (incl. IPv4-mapped), and the bare-integer IPv4
    forms a bypass uses: decimal (2130706433), hex (0x7f000001), octal
    (017700000001). Returns None when `host` is not an IP literal.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # Bare-number IPv4: decimal / 0x-hex / 0o|0-octal. int(s, 0) covers
    # 0x/0o prefixes; legacy leading-zero octal needs an explicit base 8.
    for parse in (lambda s: int(s, 0), lambda s: int(s, 8) if s.startswith("0") and s.isdigit() else None):
        try:
            n = parse(host)
        except (ValueError, TypeError):
            continue
        if n is None:
            continue
        if 0 <= n <= 0xFFFFFFFF:
            try:
                return ipaddress.ip_address(n)
            except ValueError:
                continue
    return None


def _is_blocked_host(host: str) -> bool:
    """Canonicalized-IP + textual loopback / RFC-1918 / link-local match.

    P1-2/4: before the textual prefix checks, attempt
    `ipaddress.ip_address()` canonicalization so numeric/hex/octal IPv4
    (http://2130706433/, http://0x7f000001/) and IPv6-mapped loopback
    (http://[::ffff:127.0.0.1]/) are caught — the prefix-only check missed
    these. Textual checks remain as the outer layer for hostnames.

    No DNS resolution — matches REQ-AGENT-SEC-URL-001's enumerated set.
    """
    if host in {"localhost", "::1"}:
        return True
    # Canonicalization layer — handles decimal/hex/octal IPv4 and IPv6 forms.
    # urlparse strips IPv6 brackets already; strip defensively if present.
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    ip = _canonical_ip(candidate)
    if ip is not None:
        return _ip_is_blocked(ip)
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
    # P1-2/4: source the image URL ONLY from ctx (the real pin/og:image URL
    # resolved pre-agent by the resolve_image node), never from LLM args —
    # mirrors search_products. `args` is intentionally ignored for the URL.
    image_url = str(ctx.get("image_url") or "").strip()
    if not image_url:
        return AnalyzeImageResult(ok=False, error="missing_image_url")

    # SPEC-AGENT-UX-P0-001 / REQ-UX-004 — 사전 안내 멘트 ("이미지 들여다볼게요").
    # Vision LLM 호출 직전, fail-open.
    try:
        from app.channels.pre_messages import fire_pre_message
        from app.graphs.nodes._adapter_ctx import _adapter_var

        await fire_pre_message(
            _adapter_var.get(),
            ctx,
            key="analyze_image",
            lang=ctx.get("lang") or "en",
            chat_id=ctx.get("chat_id"),
        )
    except Exception:  # noqa: BLE001 — never block analyze pipeline
        logger.debug("[tool.analyze_image] pre-message skipped")

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
