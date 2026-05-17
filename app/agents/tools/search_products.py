"""SPEC-AGENT-V2-REACT / T-003b — `search_products` tool wrapper.

Routes to one of two EXISTING search entrypoints — never a new algorithm:

- Photo-pick path: a real resolved image URL is present in ``ctx`` (pin /
  og:image resolved by the upstream resolve_image step). Use the full
  ``run_pipeline`` (dense image embedding + sparse pgroonga + RRF).
- Text-only path: no real image at all. Build a ``PipelineState`` with a
  zero dense vector and drive the SAME ``search_step`` + ``diversify_step``
  the pipeline uses. The pgroonga sparse branch carries the query; the zero
  dense vector contributes only noise that RRF + diversify rank below the
  real sparse hits (verified against the live 78k catalog).

The LLM NEVER supplies an image URL (the arg is removed from the tool
schema). Imagery is sourced internally from ``ctx`` only — this kills the
fabricated-placeholder-URL → Modal regression.

@MX:NOTE: [AUTO] Side effect: DB RPC (text path) or DB RPC + Modal embed (photo path).
@MX:SPEC: SPEC-AGENT-V2-REACT
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.agents.tool_registry import SearchProductsResult
from app.core.config import settings

logger = logging.getLogger(__name__)

# Number of FashionSigLIP dims (Modal /embed). The text-only path injects a
# zero vector of this width so dense never matches anything meaningful and
# the sparse (pgroonga) branch fully drives ranking.
_EMBED_DIM = 768

# RFC 2606 `.invalid` TLD — provably non-resolvable. Used ONLY to satisfy the
# unmodifiable RecommendRequest.image_url field on the text-only path. It is
# NEVER sent to Modal (embed_step is bypassed; embedding is injected directly).
_TEXT_ONLY_SENTINEL = "https://text-only.invalid/none"


def _is_real_image_url(value: Any) -> bool:
    """A usable, externally-resolved image URL (pin / og:image / R2).

    Rejects empties, the text-only sentinel, and anything not http(s).
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or v == _TEXT_ONLY_SENTINEL:
        return False
    return v.startswith(("http://", "https://"))


def _to_card_candidate(cand: Any) -> Any:
    """Normalize a pipeline result into a `Candidate` model for the card path.

    The text-only path returns raw RPC dicts (`final_candidates`); the photo
    path returns `Candidate` models (run_pipeline already converts). The V1
    card renderer (`send_results._candidate_to_card`) does attribute access,
    so dicts must be promoted to `Candidate` first. Reuses the EXACT field
    mapping `pipeline.runner` uses (single source of truth for that shape).
    Returns the original object on failure (best-effort; rendered-or-skipped
    downstream by `_candidate_to_card`).
    """
    if hasattr(cand, "image_url") and not isinstance(cand, dict):
        return cand  # already a Candidate-like model
    if not isinstance(cand, dict):
        return cand
    try:
        from app.models.response import Candidate

        return Candidate(
            id=str(cand["id"]),
            brand=cand.get("brand", ""),
            name=cand.get("name", ""),
            price=cand.get("price"),
            image_url=cand.get("image_url"),
            product_url=cand.get("product_url"),
            platform=cand.get("platform"),
            subcategory=cand.get("subcategory"),
            score=float(cand.get("score", 0.0)),
            dense_rank=cand.get("dense_rank"),
            sparse_rank=cand.get("sparse_rank"),
        )
    except Exception:  # noqa: BLE001
        return cand


# Per-turn marker set in the shared ctx dict when a search ran THIS turn AND
# returned >0 candidates. `respond` sends cards ONLY when this is set — gating
# on this (not on `sess.last_results` non-emptiness) is what stops the stale
# previous-turn cards from leaking onto greeting/chit-chat/clarify turns.
# `ctx` is built once per turn in react_loop and shared by reference across
# search_products / refine_search / respond dispatch (react_loop.py:301+524).
CARDS_READY_KEY = "_cards_ready_this_turn"


def persist_last_results(ctx: dict[str, Any], cands: list[Any]) -> int:
    """Stash the FULL turn candidates into the session so `respond` can render
    real product cards internally (LLM never hand-serializes cards).

    Reuses the EXISTING V1 session field `sess.last_results` (the same field
    `send_results` populates and critique callbacks consume) — no new state.
    Also extends `shown_product_ids` for parity with the V1 send path. On a
    successful persist (>0 candidates) sets the per-turn `CARDS_READY_KEY`
    marker in `ctx` so `respond` knows a search ran THIS turn (a 0-result
    search leaves the marker unset → respond sends text only, no stale cards).
    Returns the number of candidates persisted.
    """
    chat_id = ctx.get("chat_id")
    if chat_id is None or not cands:
        return 0
    try:
        from app.channels.session import get_store

        store = get_store()
        sess = store.get_or_create(int(chat_id))
        normalized = [_to_card_candidate(c) for c in cands]
        sess.last_results = list(normalized)
        new_ids = [str(getattr(c, "id", "") or "") for c in normalized]
        new_ids = [i for i in new_ids if i]
        sess.shown_product_ids = list(dict.fromkeys(sess.shown_product_ids + new_ids))
        store.update(sess)
        # Mark THIS turn as card-bearing only when we actually persisted
        # candidates (>0). react_loop shares this `ctx` with `respond`.
        if normalized:
            ctx[CARDS_READY_KEY] = True
        return len(normalized)
    except Exception as exc:  # noqa: BLE001 — never break the search tool
        logger.debug("[tool.search_products] persist_last_results failed: %r", exc)
        return 0


def apply_dislike_discount(ctx: dict[str, Any], cands: list[Any]) -> list[Any]:
    """SPEC-AGENT-V3-REACT Gap4 — flag-gated cross-thread dislike discount.

    flag OFF → returns `cands` unchanged (V2 byte-identical; no store read).
    flag ON → reads the user's TasteProfile recency-weighted dislike excludes
    and drops candidates whose brand/title matches — REUSING the EXACT same
    client-side title/brand filter pattern `refine_search` already applies for
    `exclude_keywords`. No new ranking, no new search algorithm.

    @MX:NOTE: [AUTO] additive — reuses the existing exclude filter pattern,
      flag-gated, no new ranking.
    @MX:SPEC: SPEC-AGENT-V3-REACT
    """
    if not settings.AGENT_V3_DISLIKE_MEMORY_ENABLED or not cands:
        return cands
    user_key = ctx.get("user_key")
    if not user_key:
        return cands
    try:
        from app.channels.taste_profile import get_taste_store

        profile = get_taste_store().get_or_create(user_key)
        ex_brands, ex_keywords = profile.recency_weighted_excludes(time.time())
    except Exception as exc:  # noqa: BLE001 — never break search
        logger.debug("[tool.search_products] dislike discount skipped: %r", exc)
        return cands
    if not ex_brands and not ex_keywords:
        return cands
    eb = {b.lower() for b in ex_brands}
    ek = {k.lower() for k in ex_keywords}

    def _keep(c: Any) -> bool:
        brand = (getattr(c, "brand", "") or "").lower()
        if brand and brand in eb:
            return False
        title = (getattr(c, "title", "") or getattr(c, "name", "") or "").lower()
        return not any(k in title for k in ek)

    return [c for c in cands if _keep(c)]


def _candidate_to_dict(cand: Any) -> dict[str, Any]:
    """Best-effort serialization of a Candidate to a small LLM-consumable dict."""
    try:
        if hasattr(cand, "model_dump"):
            d = cand.model_dump()
        elif isinstance(cand, dict):
            d = dict(cand)
        else:
            d = {k: getattr(cand, k, None) for k in ("product_id", "id", "brand", "title", "name", "price")}
    except Exception:  # noqa: BLE001
        d = {}
    # Pipeline dicts + Candidate models both expose `name`; `title` was only a
    # guess and is empty for the text-only path — fall back to `name` so the
    # LLM actually sees product names in its context.
    return {
        "product_id": d.get("product_id") or d.get("id"),
        "brand": d.get("brand"),
        "title": (d.get("title") or d.get("name") or "")[:80],
        "price": d.get("price"),
    }


async def run_text_only_search(
    *,
    text_query: str,
    category: str | None = None,
    fit: str | None = None,
    color_family: str | None = None,
    top_k: int = 15,
) -> list[Any]:
    """Text/sparse-only search — reuses the EXISTING search_step + diversify_step.

    No image, no Modal call. A zero dense vector is injected so the RPC's
    pgroonga (sparse) branch fully drives ranking; RRF + diversify push the
    zero-vector dense noise below the real sparse hits. Shared by
    `search_products` and `refine_search` text-only paths.

    Returns the diversified candidate dicts (pipeline `final_candidates`).
    """
    from app.models.request import AnalyzedItem, RecommendRequest
    from app.pipeline.diversify import diversify_step
    from app.pipeline.search import search_step
    from app.pipeline.state import PipelineState

    item = AnalyzedItem(
        id="agent-v2-text",
        category=category or "apparel",
        subcategory=None,
        fit=fit,
        color_family=color_family,
        # Force the English/free-text query into the sparse slot. We
        # deliberately do NOT set search_query_ko so search_step uses this
        # value verbatim against the (English-indexed) pgroonga column.
        search_query=text_query,
    )
    req = RecommendRequest(item=item, image_url=_TEXT_ONLY_SENTINEL, final_limit=max(1, int(top_k)))
    state = PipelineState(request=req)
    # Bypass embed_step entirely — sentinel URL never reaches Modal.
    state.embedding = [0.0] * _EMBED_DIM
    state = await search_step(state)
    state = await diversify_step(state)
    return list(state.final_candidates or [])


async def run_image_search(
    *,
    image_url: str,
    text_query: str,
    category: str | None = None,
    fit: str | None = None,
    color_family: str | None = None,
    top_k: int = 15,
) -> list[Any]:
    """Photo-pick path — full existing `run_pipeline` (dense image + sparse + RRF).

    `image_url` MUST be an externally-resolved URL sourced from ctx (never an
    LLM arg, never a placeholder).
    """
    from app.models.request import AnalyzedItem, RecommendRequest
    from app.pipeline.runner import run_pipeline

    item = AnalyzedItem(
        id="agent-v2",
        category=category or "apparel",
        subcategory=None,
        fit=fit,
        color_family=color_family,
        search_query=text_query,
    )
    req = RecommendRequest(item=item, image_url=image_url, final_limit=max(1, int(top_k)))
    resp = await run_pipeline(req)
    return list(getattr(resp, "results", None) or getattr(resp, "candidates", None) or [])


async def dispatch(args: dict[str, Any], ctx: dict[str, Any]) -> SearchProductsResult:
    text_query = (args.get("text_query") or "").strip()
    ctx_image = ctx.get("image_url")
    has_image = _is_real_image_url(ctx_image)

    # A non-empty text_query alone is sufficient. Only a turn with neither a
    # query nor a usable image is unanswerable.
    if not text_query and not has_image:
        return SearchProductsResult(ok=False, error="no_query", candidates_count=0, top_candidates=[])

    top_k = int(args.get("top_k") or 15)
    category = args.get("style_node_primary")
    fit = args.get("fit")
    color_family = args.get("color_family")

    try:
        if has_image:
            # Photo-pick: real resolved image drives dense; text_query (or the
            # category) seeds sparse. NEVER an LLM-supplied / placeholder URL.
            query = text_query or category or "fashion item"
            cands = await run_image_search(
                image_url=str(ctx_image),
                text_query=query,
                category=category,
                fit=fit,
                color_family=color_family,
                top_k=top_k,
            )
        else:
            cands = await run_text_only_search(
                text_query=text_query,
                category=category,
                fit=fit,
                color_family=color_family,
                top_k=top_k,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tool.search_products] pipeline raised: %r", exc)
        return SearchProductsResult(
            ok=False, error=f"pipeline_failed:{type(exc).__name__}", candidates_count=0, top_candidates=[]
        )

    # SPEC-AGENT-V3-REACT Gap4 — merge cross-thread dislike before persisting
    # (flag-gated; OFF → cands unchanged → V2 byte-identical).
    cands = apply_dislike_discount(ctx, cands)

    # Persist FULL candidates for the turn so `respond` can render real cards
    # internally (the LLM never hand-serializes cards). LLM context still gets
    # only the small `top_candidates` summary below.
    persist_last_results(ctx, cands)

    top = [_candidate_to_dict(c) for c in cands[:5]]
    return SearchProductsResult(ok=True, error=None, candidates_count=len(cands), top_candidates=top)
