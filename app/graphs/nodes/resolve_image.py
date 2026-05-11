"""SPEC-AGENT-001 / REQ-AGENT-004 (node 2/10) — resolve_image.

Wraps: `app/channels/link_resolver.py::resolve` (Pinterest / og:image).

For direct photo uploads (photo_file_id present), the graph treats this as
"unsupported" — same behavior as the original `scenario.handle_new_image`
PHOTO_DIRECT_NOT_SUPPORTED path. We don't burn vision tokens on bytes.
"""

from __future__ import annotations

import logging

from app.channels import link_resolver
from app.channels.session import get_store
from app.graphs.state import WorkingState
from app.observability.langfuse import observe

logger = logging.getLogger(__name__)


@observe(name="node.resolve_image", as_type="span")
async def resolve_image(state: WorkingState) -> dict:
    msg = state.message
    breadcrumbs: list[str] = []

    if msg.photo_file_id and not msg.urls:
        # Direct upload — not supported. Leave image_url=None; the routing
        # function sends the user to `respond` which uses the photo-upload
        # fallback template.
        breadcrumbs.append("resolve_image: photo_file_id only — direct upload not supported")
        return {"image_url": None, "log_events": breadcrumbs}

    if not msg.urls:
        breadcrumbs.append("resolve_image: no urls and no photo")
        return {"image_url": None, "log_events": breadcrumbs}

    for u in msg.urls:
        try:
            images = await link_resolver.resolve(str(u))
        except Exception as exc:  # REQ-AGENT-007
            logger.exception("[resolve_image] link_resolver.resolve raised")
            breadcrumbs.append(f"resolve_image_error: {type(exc).__name__}: {exc}"[:200])
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
            return {"image_url": images[0], "log_events": breadcrumbs}

    breadcrumbs.append("resolve_image: all urls failed → image_url=None")
    return {"image_url": None, "log_events": breadcrumbs}
