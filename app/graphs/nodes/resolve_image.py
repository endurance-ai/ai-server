"""SPEC-AGENT-001 / REQ-AGENT-004 (node 2/10) — resolve_image.

Wraps: `app/channels/link_resolver.py::resolve` (Pinterest / og:image).

For direct photo uploads (photo_file_id present), the graph treats this as
"unsupported" — same behavior as the original `scenario.handle_new_image`
PHOTO_DIRECT_NOT_SUPPORTED path. We don't burn vision tokens on bytes.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from app.channels import link_resolver
from app.graphs.nodes._trace import node_done, node_enter, node_skip
from app.graphs.state import WorkingState
from app.infrastructure.memory.session import get_store
from app.infrastructure.memory.taste_profile import user_key_for
from app.observability.conversation_log import emit
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


# @MX:SPEC: SPEC-CONVERSATION-LOG-001
def _emit_link_resolved(
    state: WorkingState,
    input_url: str,
    resolved_image_url: str | None,
) -> None:
    """LOG-T12 — emit `link_resolved` (single image / og:image branch)."""
    try:
        host = urlparse(input_url).hostname or ""
        is_pinterest = host == "pin.it" or host.endswith("pinterest.com")
        event_type = "pinterest_ingest" if is_pinterest else "link_resolved"
        if event_type == "pinterest_ingest":
            payload = {
                "mode": "pin",
                "pin_count": 1 if resolved_image_url else 0,
                "vision_results_count": 0,
            }
        else:
            payload = {
                "input_url": input_url,
                "resolved_image_url": resolved_image_url,
                "host": host,
            }
        emit(
            event_type=event_type,
            user_key=user_key_for(state.from_user_id, state.chat_id),
            chat_id=state.chat_id,
            thread_id=state.thread_id,
            turn_no=2,
            payload=payload,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[resolve_image] link_resolved emit best-effort")


@observe(name="node.resolve_image", as_type="span")
async def resolve_image(state: WorkingState) -> dict:
    node_enter("resolve_image")
    msg = state.message
    breadcrumbs: list[str] = []

    if msg.photo_file_id and not msg.urls:
        # Direct upload — not supported. Leave image_url=None; the routing
        # function sends the user to `respond` which uses the photo-upload
        # fallback template.
        breadcrumbs.append("resolve_image: photo_file_id only — direct upload not supported")
        node_skip("resolve_image", "direct upload not supported")
        return {"image_url": None, "log_events": breadcrumbs, "turn_no": 2}

    if not msg.urls:
        breadcrumbs.append("resolve_image: no urls and no photo")
        node_skip("resolve_image", "no urls and no photo")
        return {"image_url": None, "log_events": breadcrumbs, "turn_no": 2}

    for u in msg.urls:
        url_str = str(u)
        try:
            images = await link_resolver.resolve(url_str)
        except Exception as exc:  # REQ-AGENT-007
            logger.exception("[resolve_image] link_resolver.resolve raised")
            breadcrumbs.append(f"resolve_image_error: {type(exc).__name__}: {exc}"[:200])
            _emit_link_resolved(state, url_str, None)
            continue
        if images:
            # REQ-COMPAT-002 — persist to session so subsequent turns
            # (picker tap, intent reply, refine) can run search_node which
            # reads sess.image_url. Without this, search_node short-circuits
            # at "no image_url; cannot search".
            try:
                sess = get_store().get_or_create(state.chat_id)
                sess.image_url = images[0]
                get_store().update(sess)
            except Exception:
                logger.exception("[resolve_image] session persist failed")
            breadcrumbs.append(f"resolve_image: ok url={images[0][:80]}")
            _emit_link_resolved(state, url_str, images[0])
            node_done("resolve_image", resolved="ok")
            return {"image_url": images[0], "log_events": breadcrumbs, "turn_no": 2}
        # Resolver returned no images for this URL — record the failure too.
        _emit_link_resolved(state, url_str, None)

    breadcrumbs.append("resolve_image: all urls failed → image_url=None")
    node_skip("resolve_image", "all urls failed")
    return {"image_url": None, "log_events": breadcrumbs, "turn_no": 2}
