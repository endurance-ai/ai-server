"""SPEC-AGENT-V2-REACT / T-003b — `search_products` tool wrapper.

Routes to one of two EXISTING search entrypoints — never a new algorithm:

- Photo-pick path: a real resolved image URL is present in ``ctx`` (pin /
  og:image resolved by the upstream resolve_image step). Use the full
  ``run_pipeline`` (image embedding → v6 RPC).
- Text-only path: no real image at all. SPEC-SEARCH-V6-001: build a
  ``PipelineState`` with a REAL text embedding (``EmbedProvider.embed_text``
  — same FashionSigLIP L2 space, cross-modal cosine valid) and drive the SAME
  ``search_step`` + ``diversify_step``. v6 is embedding-first; the old
  zero-dense + pgroonga sparse trick is gone (pgroonga/v5 were dropped).

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

logger = logging.getLogger(__name__)

# RFC 2606 `.invalid` TLD — provably non-resolvable. Used ONLY to satisfy the
# required RecommendRequest.image_url field on the text-only path. It is NEVER
# sent to Modal (embed_step is bypassed; the text embedding is injected via
# EmbedProvider.embed_text directly — SPEC-SEARCH-V6-001).
_TEXT_ONLY_SENTINEL = "https://text-only.invalid/none"

# 260522 gender pin (SPEC-GENDER-PIN-001) — recognized gender tokens (English;
# the LLM canonical-form is always English; Korean gender words never reach
# here — the prompt + the English `suggested_query` keep text_query English).
_GENDER_TOKENS = ("men", "women", "unisex")


def emit_search_done(
    *,
    ctx: dict[str, Any] | None = None,
    chat_id: int | None = None,
    user_key: str | None = None,
    thread_id: Any = None,
    turn_no: int | None = None,
    text_query: str,
    cands: list[Any],
    category: str | None = None,
    is_refine: bool = False,
) -> None:
    """Emit a `search_done` conversation-log event. Never raises.

    260611 — `search_done` was defined in the event catalog but never actually
    emitted, so `get_recent_history` (and the agent's memory injection that
    wraps it) had no record of prior searches. The LLM then could not detect
    that a refine (e.g. "더 저렴한 걸") had a target to refine — it called
    `get_recent_history`, got nothing, and replied "기록이 없어서…".

    `ctx` is used as the primary source (search_products / refine_search
    dispatch path); explicit kwargs override (e.g. the inline
    `_handle_gender_pick` path which has only `state`).
    """
    try:
        from app.observability.conversation_log import emit

        if ctx is not None:
            chat_id = chat_id if chat_id is not None else ctx.get("chat_id")
            user_key = user_key if user_key is not None else ctx.get("user_key")
            thread_id = thread_id if thread_id is not None else ctx.get("thread_id")
        if chat_id is None or user_key is None:
            return  # missing minimal identity — skip silently
        top_ids: list[str] = []
        for c in (cands or [])[:5]:
            try:
                pid = getattr(c, "id", None)
                if pid is None and isinstance(c, dict):
                    pid = c.get("id")
                if pid:
                    top_ids.append(str(pid))
            except Exception:  # noqa: BLE001 — best-effort projection
                continue
        emit(
            event_type="search_done",
            user_key=str(user_key),
            chat_id=int(chat_id),
            thread_id=thread_id,
            turn_no=turn_no,
            payload={
                "query": {"text_query": (text_query or "")[:240]},
                "top_k_product_ids": top_ids,
                "dense_count": len(cands or []),
                "filters": {"category": category} if category else {},
                "is_refine": bool(is_refine),
            },
        )
    except Exception as exc:  # noqa: BLE001 — observability is best-effort
        logger.debug("[search_done emit] skip: %r", exc)


def _query_gender(text_query: str) -> str | None:
    """Return the gender token present in `text_query` (whole-word), else None."""
    tokens = text_query.lower().split()
    for g in _GENDER_TOKENS:
        if g in tokens:
            return g
    return None


def pipeline_exc_detail(exc: BaseException, *, include_host: bool) -> str:
    """Render a `pipeline_failed:` suffix from an exception (review P1-C 260522).

    Shared by `search_products` + `refine_search` (was duplicated). Pulls the
    HTTP status code and, when `include_host=True`, the target host so an
    operator can tell Modal cold-start from PostgREST 5xx at a glance.

    `include_host` is True for the LOG line (full diagnostic) and False for the
    value returned in `Result.error` (security 260522: the host is internal
    infra — Modal endpoint / PostgREST shim — and `Result.error` can transit to
    the LLM context / user, so the host stays log-only). The status code is
    kept in both (it is the key signal and not sensitive).
    """
    detail = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    if isinstance(status, int):
        detail = f"{detail}:{status}"
    if include_host:
        req = getattr(exc, "request", None)
        url_obj = getattr(req, "url", None)
        host = getattr(url_obj, "host", None) if url_obj is not None else None
        if isinstance(host, str) and host:
            detail = f"{detail}@{host}"
    return detail


def _lookup_profile_gender(ctx: dict[str, Any]) -> str | None:
    """Read the user's PINNED gender from the taste profile (cross-session).

    Returns 'men'/'women'/'unisex' or None when never pinned. Best-effort —
    any failure (no user_key, store error) → None (treated as "ask")."""
    user_key = ctx.get("user_key")
    if not user_key:
        return None
    try:
        from app.infrastructure.memory.taste_profile import get_taste_store

        profile = get_taste_store().get_or_create(user_key)
        g = (getattr(profile, "gender", None) or "").strip().lower()
        return g if g in _GENDER_TOKENS else None
    except Exception:  # noqa: BLE001
        return None


async def _send_gender_card(ctx: dict[str, Any], *, lang: str) -> bool:
    """Send the one-time [남성][여성][상관없음] gender card. Returns True on send.

    Callback shape `clarify:gender:{men|women|unisex}` is consumed inline by
    `ingest` (SPEC-GENDER-PIN-001) — it pins the choice to taste_profile and
    re-runs the pending search. Best-effort: any failure → False (caller then
    falls back to a unisex search rather than dead-ending the turn)."""
    chat_id = ctx.get("chat_id")
    if chat_id is None:
        return False
    try:
        from app.graphs.nodes._adapter_ctx import get_adapter

        adapter = get_adapter()
        if lang == "ko":
            prompt = "누가 입을 거야? 한 번만 알려주면 다음부터 딱 맞게 골라줄게 🐱"
            buttons = [
                [("👔 남성", "clarify:gender:men")],
                [("👗 여성", "clarify:gender:women")],
                [("🙆 상관없음", "clarify:gender:unisex")],
            ]
        else:
            prompt = "Who's it for? Tell me once and I'll tune every pick from here 🐱"
            buttons = [
                [("👔 Men", "clarify:gender:men")],
                [("👗 Women", "clarify:gender:women")],
                [("🙆 Either", "clarify:gender:unisex")],
            ]
        if hasattr(adapter, "send_text_with_keyboard"):
            await adapter.send_text_with_keyboard(chat_id, prompt, buttons)
        elif hasattr(adapter, "send_text_with_buttons"):
            flat = [b[0] for b in buttons]
            await adapter.send_text_with_buttons(chat_id, prompt, flat)
        else:
            await adapter.send_text(chat_id, prompt)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[tool.search_products] gender card send failed: %r", exc)
        return False


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

        # v6 rows carry `distance` (cosine, ASC=better). score = 1.0 - distance
        # preserves the downstream "higher=better, RPC order" semantics;
        # str(bigint int) is stable so dedup/like callbacks are unaffected
        # (SPEC-SEARCH-V6-001).
        return Candidate(
            id=str(cand["id"]),
            brand=cand.get("brand", ""),
            name=cand.get("name", ""),
            price=cand.get("price"),
            image_url=cand.get("image_url"),
            product_url=cand.get("product_url"),
            platform=cand.get("platform"),
            subcategory=cand.get("subcategory"),
            score=float(1.0 - cand.get("distance", 1.0)),
            dense_rank=None,
            sparse_rank=None,
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
        from app.infrastructure.memory.session import get_store

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


def apply_price_filter(
    cands: list[Any],
    min_price: float | None,
    max_price: float | None,
) -> list[Any]:
    """Client-side price bound filter (SPEC-SEARCH-V6-001 + 2026-05-20 user
    requirement).

    pgvector + HNSW does not push-down range predicates cleanly — the
    canonical pattern is "fetch wider, filter in app" so vector recall stays
    intact. The RPC therefore stays price-agnostic and we trim here.

    Policy:
      - When neither bound is set, returns `cands` unchanged.
      - When at least one bound is set, candidates without a usable numeric
        `price` field are DROPPED (treat absent price as out-of-bounds — the
        user's price constraint is explicit, so silently keeping un-priced
        rows would violate intent).
      - Bounds are inclusive. KRW assumed throughout the repo (Candidate
        prices, summary formatting, and LLM-supplied args all denominate in
        KRW integer 원).
    """
    if min_price is None and max_price is None:
        return cands
    lo = float(min_price) if min_price is not None else None
    hi = float(max_price) if max_price is not None else None
    kept: list[Any] = []
    for c in cands:
        raw = getattr(c, "price", None)
        if raw is None and isinstance(c, dict):
            raw = c.get("price")
        try:
            p = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            p = None
        if p is None:
            continue
        if lo is not None and p < lo:
            continue
        if hi is not None and p > hi:
            continue
        kept.append(c)
    dropped = len(cands) - len(kept)
    if dropped > 0:
        logger.info(
            "💰 [price_filter] kept=%d dropped=%d min=%s max=%s",
            len(kept),
            dropped,
            min_price,
            max_price,
        )
    return kept


def apply_dislike_discount(ctx: dict[str, Any], cands: list[Any]) -> list[Any]:
    """SPEC-AGENT-V3-REACT Gap4 — flag-gated cross-thread dislike discount.

    SPEC-AGENT-V2-CLEANUP-001 — cross-thread dislike discount is now
    UNCONDITIONAL (the AGENT_V3_DISLIKE_MEMORY_ENABLED flag was removed).
    Reads the user's TasteProfile recency-weighted dislike excludes and drops
    candidates whose brand/title matches — REUSING the EXACT same client-side
    title/brand filter pattern `refine_search` already applies for
    `exclude_keywords`. No new ranking, no new search algorithm.

    @MX:NOTE: [AUTO] additive — reuses the existing exclude filter pattern,
      no new ranking.
    """
    if not cands:
        return cands
    user_key = ctx.get("user_key")
    if not user_key:
        return cands
    try:
        from app.infrastructure.memory.taste_profile import get_taste_store

        profile = get_taste_store().get_or_create(user_key)
        ex_brands, ex_keywords = profile.recency_weighted_excludes(time.time())
    except Exception as exc:  # noqa: BLE001 — never break search
        logger.debug("[tool.search_products] dislike discount skipped: %r", exc)
        return cands
    if not ex_brands and not ex_keywords:
        logger.info("🚫 [v3:dislike] skip · no recency-weighted excludes")
        return cands
    eb = {b.lower() for b in ex_brands}
    ek = {k.lower() for k in ex_keywords}

    def _keep(c: Any) -> bool:
        brand = (getattr(c, "brand", "") or "").lower()
        if brand and brand in eb:
            return False
        title = (getattr(c, "title", "") or getattr(c, "name", "") or "").lower()
        return not any(k in title for k in ek)

    kept = [c for c in cands if _keep(c)]
    logger.info(
        "🚫 [v3:dislike] discounted brands=%d kw=%d dropped=%d",
        len(eb),
        len(ek),
        len(cands) - len(kept),
    )
    return kept


# SPEC-SEARCH-V6-001: the zero-dense stopgap (_is_zero_dense_noise /
# _suppress_zero_dense_noise / the _EMBED_DIM zero-vector injection / the
# "embedding all-zero" suppression block) was DELETED — not a silent
# safety-net removal. Its sole precondition was a zero query vector, which the
# old v5 + pgroonga text path injected so the sparse branch carried the query.
# Under v6 the text path sends a REAL embed_text() vector (no pgroonga, no
# zero vector), so the precondition can never hold; the filter would be dead
# code. v6's embedding-first ranking + distance ASC ordering already places
# the genuinely relevant rows on top, so the stopgap is obsolete by design.


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
    """Text-only search — reuses the EXISTING search_step + diversify_step.

    SPEC-SEARCH-V6-001: no image, no Modal IMAGE call. A REAL text embedding
    is injected via ``EmbedProvider.embed_text`` (same FashionSigLIP L2 space
    — cross-modal cosine valid), then the SAME v6 ``search_step`` +
    ``diversify_step`` run. embed_step is still bypassed; the sentinel URL is
    retained ONLY to satisfy the required RecommendRequest.image_url field and
    is NEVER sent to Modal. Shared by `search_products` and `refine_search`
    text-only paths.

    Returns the diversified candidate dicts (pipeline `final_candidates`).
    """
    from app.models.request import AnalyzedItem, RecommendRequest
    from app.pipeline.diversify import diversify_step

    # EmbedProvider is re-exported by app.pipeline.embed (the same monkeypatch
    # seam the characterization net uses for embed_image_url), so the text
    # embedding goes through the consistent codebase seam.
    from app.pipeline.embed import EmbedProvider
    from app.pipeline.search import search_step
    from app.pipeline.state import PipelineState

    # SPEC-SEARCH-V6-001: carry the REAL Vision/text category into the item so
    # search_service → build_params normalizes it to a canonical 20-family
    # token. `AnalyzedItem.category` is a required str: when there is no Vision
    # item (`category is None`) keep the legacy "apparel" placeholder — it
    # normalizes to `other` (gate skipped), the correct graceful degrade.
    item = AnalyzedItem(
        id="agent-v2-text",
        category=category or "apparel",
        subcategory=None,
        fit=fit,
        color_family=color_family,
        search_query=text_query,
    )
    req = RecommendRequest(item=item, image_url=_TEXT_ONLY_SENTINEL, final_limit=max(1, int(top_k)))
    state = PipelineState(request=req)
    # Bypass embed_step (image path) — inject a REAL text embedding instead.
    # The sentinel URL never reaches Modal.
    # Invariant guard (review P1-1): both current callers already ensure a
    # non-empty query (search_products.dispatch no_query gate; refine_search
    # `or "fashion"`), but this helper is shared/public — embedding an empty
    # string is meaningless, so fail fast & explicitly rather than POST "" to
    # Modal. Caught by the caller's dispatch try/except → ok=False.
    if not text_query.strip():
        raise ValueError("run_text_only_search requires a non-empty text_query")
    logger.info(
        "🔍 [text_search] embed text_query=%r category=%r fit=%r color_family=%r top_k=%d",
        text_query[:120],
        category,
        fit,
        color_family,
        max(1, int(top_k)),
    )
    # 260522 per-step timing — the text-only path was opaquely slow (live: a
    # single search_products took 29.7s with a Modal embed timeout+retry). Time
    # each stage so the bottleneck is attributable from logs alone:
    #   ⏱ embed  = Modal /embed/text (cold-start prone; cache hit ≈ 0ms)
    #   ⏱ rpc    = search_step (PostgREST RPC + family gate)
    #   ⏱ divers = diversify_step (brand/platform/content caps)
    _t_embed0 = time.perf_counter()
    state.embedding = await EmbedProvider.embed_text(text_query)
    _embed_ms = int((time.perf_counter() - _t_embed0) * 1000)

    _t_rpc0 = time.perf_counter()
    state = await search_step(state)
    _rpc_ms = int((time.perf_counter() - _t_rpc0) * 1000)

    _t_div0 = time.perf_counter()
    state = await diversify_step(state)
    _div_ms = int((time.perf_counter() - _t_div0) * 1000)

    logger.info(
        "🔍 [text_search] done text_query=%r → final=%d · ⏱ embed=%dms rpc=%dms divers=%dms total=%dms",
        text_query[:120],
        len(state.final_candidates or []),
        _embed_ms,
        _rpc_ms,
        _div_ms,
        _embed_ms + _rpc_ms + _div_ms,
    )
    return list(state.final_candidates or [])


async def run_blended_search(
    *,
    origin_url: str,
    modifier_query: str,
    chat_id: int | None = None,
    alpha: float = 0.7,
    category: str | None = None,
    fit: str | None = None,
    color_family: str | None = None,
    top_k: int = 15,
) -> list[Any]:
    """Multi-turn blended search (Level 1 image-first refinement).

    Blends the stored origin image vector with the text modifier embedding so
    follow-up turns ("more casual", "different colour") preserve the original
    outfit identity instead of re-embedding a raw refinement instruction.

    The image vector is lazily embedded on the first refine turn and cached in
    `origin_image` for subsequent calls -- avoiding extra Modal calls at Vision time.

    query_vec = normalize(alpha * origin_image_vec + (1-alpha) * modifier_text_vec)
    """
    from app.agents.origin_image import blend_vectors, get_origin_vector, set_origin_vector
    from app.models.request import AnalyzedItem, RecommendRequest
    from app.pipeline.diversify import diversify_step
    from app.pipeline.embed import EmbedProvider
    from app.pipeline.search import search_step
    from app.pipeline.state import PipelineState

    _t0 = time.perf_counter()
    modifier_query = modifier_query.strip() or "fashion"

    origin_vec = get_origin_vector(chat_id) if chat_id is not None else None
    if origin_vec is None:
        origin_vec = await EmbedProvider.embed_image_url(origin_url)
        if origin_vec and chat_id is not None:
            set_origin_vector(chat_id, origin_vec)

    if not origin_vec:
        return await run_text_only_search(
            text_query=modifier_query,
            category=category,
            fit=fit,
            color_family=color_family,
            top_k=top_k,
        )

    modifier_vec = await EmbedProvider.embed_text(modifier_query)
    _embed_ms = int((time.perf_counter() - _t0) * 1000)

    blended = blend_vectors(origin_vec, modifier_vec, alpha)

    item = AnalyzedItem(
        id="agent-blended",
        category=category or "apparel",
        subcategory=None,
        fit=fit,
        color_family=color_family,
        search_query=modifier_query,
    )
    req = RecommendRequest(item=item, image_url=_TEXT_ONLY_SENTINEL, final_limit=max(1, int(top_k)))
    state = PipelineState(request=req)
    state.embedding = blended

    _t_rpc0 = time.perf_counter()
    state = await search_step(state)
    _rpc_ms = int((time.perf_counter() - _t_rpc0) * 1000)

    _t_div0 = time.perf_counter()
    state = await diversify_step(state)
    _div_ms = int((time.perf_counter() - _t_div0) * 1000)

    logger.info(
        "🔍 [blended_search] modifier=%r alpha=%.1f cached_vec=%s → final=%d · embed=%dms rpc=%dms divers=%dms",
        modifier_query[:80],
        alpha,
        origin_vec is not None,
        len(state.final_candidates or []),
        _embed_ms,
        _rpc_ms,
        _div_ms,
    )
    return list(state.final_candidates or [])


# --- Intent-aware blended search (Level 2 advanced, 2026-06-06) ----------

# Per-intent strategy + tuning. Derived from the multi-turn eval
# (tests/eval/multiturn_*.json + beta sweep on 2026-06-06):
#
#   color_swap            -> weighted_sum alpha=0.3  (harmonic 0.382)
#                            ↳ weighted-sum beat vector_arith at every beta;
#                              direct colour token in modifier matches catalog
#                              name keywords (navy/blue/black) better.
#   fit_change            -> vector_arith beta=1.0   (harmonic 0.156)
#                            ↳ vector_arith preserves silhouette better than
#                              any alpha for fit changes; explicit subtract
#                              of the OLD fit token is what worked.
#   mood_shift            -> weighted_sum alpha=0.3  (all modes ~weak; alpha=0.3
#                                                    best harmonic)
#   identity_preservation -> weighted_sum alpha=0.55 (eval sweet spot)
#   free_form             -> weighted_sum alpha=0.5  (safe default)
_STRATEGY_BY_INTENT: dict[str, dict[str, Any]] = {
    "color_swap": {"strategy": "weighted_sum", "alpha": 0.3},
    "fit_change": {"strategy": "vector_arith", "beta": 1.0},
    "mood_shift": {"strategy": "weighted_sum", "alpha": 0.3},
    "identity_preservation": {"strategy": "weighted_sum", "alpha": 0.55},
    "free_form": {"strategy": "weighted_sum", "alpha": 0.5},
}


async def run_smart_blended_search(
    *,
    origin_url: str,
    modifier_query: str,
    chat_id: int | None = None,
    prior_outfit_context: str | None = None,
    category: str | None = None,
    fit: str | None = None,
    color_family: str | None = None,
    top_k: int = 15,
) -> list[Any]:
    """Intent-aware multi-turn search (Level 2 advanced).

    Routes the refine turn to one of two embedding strategies based on the
    LLM-classified intent of `modifier_query`:

      - color_swap / fit_change   -> VECTOR ARITHMETIC
          query = normalize(origin - beta * from_vec + beta * to_vec)
          Lets the caller explicitly drop the OLD attribute (colour or fit)
          and inject the NEW one, which weighted-sum cannot do at 0.7 because
          the original colour token dominates the embedding.

      - mood_shift / identity_preservation / free_form -> WEIGHTED SUM
          query = normalize(alpha * origin + (1-alpha) * modifier)
          Per-intent alpha from the multi-turn eval baseline (mood=0.3,
          identity_preservation=0.55, free_form=0.5).

    Any failure (intent classifier, embed call, origin lookup) falls through
    to plain text-only search so a refine turn NEVER returns nothing.

    See `tests/eval/multiturn_*.json` for the baseline data behind the
    routing decisions and per-intent alphas.
    """
    from app.agents.intent_classifier import classify_intent
    from app.agents.origin_image import blend_vectors, get_origin_vector, set_origin_vector, vector_arithmetic
    from app.models.request import AnalyzedItem, RecommendRequest
    from app.pipeline.diversify import diversify_step
    from app.pipeline.embed import EmbedProvider
    from app.pipeline.search import search_step
    from app.pipeline.state import PipelineState

    _t0 = time.perf_counter()
    modifier_query = modifier_query.strip() or "fashion"

    # 1) Origin vector (lazy embed + cache, mirrors run_blended_search).
    origin_vec = get_origin_vector(chat_id) if chat_id is not None else None
    if origin_vec is None:
        try:
            origin_vec = await EmbedProvider.embed_image_url(origin_url)
        except Exception:  # noqa: BLE001
            logger.debug("[smart_blended] origin embed failed; falling back to text-only", exc_info=True)
            origin_vec = None
        if origin_vec and chat_id is not None:
            set_origin_vector(chat_id, origin_vec)

    if not origin_vec:
        return await run_text_only_search(
            text_query=modifier_query,
            category=category,
            fit=fit,
            color_family=color_family,
            top_k=top_k,
        )

    # 2) Classify intent. On any failure → free_form (alpha=0.5 weighted sum).
    intent = await classify_intent(modifier_query, prior_outfit_context)

    # 3) Resolve strategy from the data-driven _STRATEGY_BY_INTENT table.
    rule = _STRATEGY_BY_INTENT.get(intent.intent) or _STRATEGY_BY_INTENT["free_form"]
    strategy_name = rule["strategy"]

    # vector_arith requires the classifier to return both attributes; if either
    # is missing, fall through to the safe weighted_sum default for this intent.
    use_arith = strategy_name == "vector_arith" and bool(intent.from_attribute) and bool(intent.to_attribute)

    if use_arith:
        beta = float(rule.get("beta", 1.0))
        try:
            from_vec = await EmbedProvider.embed_text(intent.from_attribute)
            to_vec = await EmbedProvider.embed_text(intent.to_attribute)
            query_vec = vector_arithmetic(origin_vec, from_vec, to_vec, beta=beta)
            strategy = f"vector_arith(β={beta}, {intent.from_attribute!r}→{intent.to_attribute!r})"
        except Exception:  # noqa: BLE001
            logger.debug("[smart_blended] arithmetic embed failed; falling back to weighted_sum", exc_info=True)
            modifier_vec = await EmbedProvider.embed_text(modifier_query)
            fallback_alpha = float(_STRATEGY_BY_INTENT["free_form"].get("alpha", 0.5))
            query_vec = blend_vectors(origin_vec, modifier_vec, fallback_alpha)
            strategy = f"weighted_sum(α={fallback_alpha}, arith_fallback)"
    else:
        alpha = float(rule.get("alpha", 0.5))
        modifier_vec = await EmbedProvider.embed_text(modifier_query)
        query_vec = blend_vectors(origin_vec, modifier_vec, alpha)
        strategy = f"weighted_sum(α={alpha})"

    _embed_ms = int((time.perf_counter() - _t0) * 1000)

    # 4) RPC + diversify (same as run_blended_search).
    item = AnalyzedItem(
        id="agent-smart-blended",
        category=category or "apparel",
        subcategory=None,
        fit=fit,
        color_family=color_family,
        search_query=modifier_query,
    )
    req = RecommendRequest(item=item, image_url=_TEXT_ONLY_SENTINEL, final_limit=max(1, int(top_k)))
    state = PipelineState(request=req)
    state.embedding = query_vec

    _t_rpc0 = time.perf_counter()
    state = await search_step(state)
    _rpc_ms = int((time.perf_counter() - _t_rpc0) * 1000)

    _t_div0 = time.perf_counter()
    state = await diversify_step(state)
    _div_ms = int((time.perf_counter() - _t_div0) * 1000)

    logger.info(
        "🔍 [smart_blended] intent=%s strategy=%s → final=%d · embed=%dms rpc=%dms divers=%dms",
        intent.intent,
        strategy,
        len(state.final_candidates or []),
        _embed_ms,
        _rpc_ms,
        _div_ms,
    )
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
    """Photo-pick path — full existing `run_pipeline` (image embedding → v6 RPC).

    `image_url` MUST be an externally-resolved URL sourced from ctx (never an
    LLM arg, never a placeholder).
    """
    from app.models.request import AnalyzedItem, RecommendRequest
    from app.pipeline.runner import run_pipeline

    # SPEC-SEARCH-V6-001: `category` is the REAL Vision garment category
    # (ctx.vision_category). It flows into AnalyzedItem → search_service →
    # build_params → to_canonical_family (the canonical 20-family gate). The
    # "apparel" fallback only applies when no Vision item is present; it
    # normalizes to `other` (gate skipped).
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
    # SPEC-AGENT-UX-P0-001 / REQ-UX-004 — 사전 안내 멘트 ("잠시만요, …찾아볼게요").
    # 본 검색 (Modal embed / DB RPC) 직전, REQ-UX-003 typing 보다 먼저 1회.
    # react_loop._fire_typing 은 dispatch 이후가 아닌 직전에 호출되므로 ordering
    # 보장을 위해 이 await 가 typing 보다 먼저 일어나도록 react_loop 가 helper
    # 분기를 통해 호출 — 여기서는 dispatch 진입 첫 줄로 await 한다.
    try:
        from app.channels.pre_messages import fire_pre_message
        from app.graphs.nodes._adapter_ctx import _adapter_var

        await fire_pre_message(
            _adapter_var.get(),
            ctx,
            key="search",
            lang=ctx.get("lang") or "en",
            chat_id=ctx.get("chat_id"),
        )
    except Exception:  # noqa: BLE001 — never block search pipeline
        logger.debug("[tool.search_products] pre-message skipped")

    text_query = (args.get("text_query") or "").strip()
    ctx_image = ctx.get("image_url")
    has_image = _is_real_image_url(ctx_image)

    # A non-empty text_query alone is sufficient. Only a turn with neither a
    # query nor a usable image is unanswerable.
    if not text_query and not has_image:
        return SearchProductsResult(ok=False, error="no_query", candidates_count=0, top_candidates=[])

    # SPEC-GENDER-PIN-001 (260522) — gender resolution before searching.
    # Priority: (1) per-request gender word the LLM put in text_query (e.g.
    # user said "여자 걸로" → 'women') OVERRIDES everything for this search;
    # (2) the user's PINNED taste_profile.gender (cross-session); (3) neither →
    # for a pure-text search, ASK once via a gender card (store the args, end
    # the turn — the clarify:gender callback re-runs this search). Image-pick
    # turns never block on the card: gender comes from Vision, and a missing
    # one falls back to 'unisex' so the pick flow is not interrupted.
    if text_query:
        explicit_gender = _query_gender(text_query)
        if explicit_gender is None:
            pinned = _lookup_profile_gender(ctx)
            if pinned:
                text_query = f"{text_query} {pinned}".strip()
            elif not has_image:
                # Unknown + never pinned + pure text → ask once. Stash the
                # search args so the callback can resume without re-typing.
                from app.agents import pending_gender

                pending_gender.set_pending(
                    ctx.get("chat_id"),
                    {
                        "text_query": text_query,
                        # Prefer the LLM-supplied category, fall back to Vision's
                        # (review P1-A 260522: stashing only vision_category
                        # dropped an explicit category on pure-text turns).
                        "category": args.get("category") or ctx.get("vision_category"),
                        "top_k": int(args.get("top_k") or 15),
                    },
                )
                sent = await _send_gender_card(ctx, lang=ctx.get("lang") or "en")
                if sent:
                    return SearchProductsResult(
                        ok=False, error="awaiting_gender", candidates_count=0, top_candidates=[]
                    )
                # Card couldn't be sent → don't dead-end; fall back to unisex.
                text_query = f"{text_query} unisex".strip()
            else:
                # Image path, no token, no pinned gender → unisex (no block).
                text_query = f"{text_query} unisex".strip()
        # else: explicit per-request gender already present → use verbatim.

    # Persist the LLM-supplied (typically English-translated) text_query into
    # ctx so a subsequent `refine_search` in the same turn / loop reuses the
    # translated form instead of the raw Korean user message that was seeded
    # by react_loop._build_ctx. Without this, refine_search rebuilds its
    # query as `<original Korean> <boost_keywords>` and the embedding picks
    # up a mixed-language string with degraded recall (live trace 2026-05-20).
    if text_query:
        ctx["text_query"] = text_query
        # 260522 cross-turn: stash the final (English, gender-pinned) product
        # query so a NEXT-TURN `refine_search` ("더 저렴하게 해줘") reuses THIS
        # query instead of embedding the raw refinement instruction (which
        # returned unrelated cheap junk — live trace 16:31). Stored only on the
        # query path; the actual >0-result guard is irrelevant (a 0-result
        # query is still the thing the user is refining).
        try:
            from app.agents.last_query import set_last_query

            set_last_query(ctx.get("chat_id"), text_query)
        except Exception:  # noqa: BLE001
            pass

    top_k = int(args.get("top_k") or 15)
    # SPEC-SEARCH-V6-001 family gate plumbing fix: the search `category` is the
    # REAL Vision garment category from ctx (`vision_category`, set in
    # react_loop._build_ctx from state.vision_selected_item / detected_items).
    # Previously this read `args.get("style_node_primary")` — a brand STYLE-NODE
    # letter (A–Z), NOT a garment category — which always normalized to `other`
    # so the v6 family gate never engaged on the primary (Telegram bot) path.
    # `style_node_primary` remains a separate concept used elsewhere; we no
    # longer mislabel it as the search category here. Text-only / no-Vision
    # turn → vision_category None → to_canonical_family → `other` → gate
    # skipped (correct graceful degrade; never fabricate a category).
    category = ctx.get("vision_category")
    fit = args.get("fit")
    color_family = args.get("color_family")

    # Multi-turn image blending (Level 1 image-first refinement):
    # when no current image URL exists but an origin image URL is stored from
    # a prior Vision turn, blend the cached image vector with the text modifier
    # so follow-up text turns ("반팔 헨리넥 찾아줘") preserve the original outfit
    # context instead of dropping to a text-only embedding. This kicks in even
    # when the LLM picked `search_products` instead of `refine_search` — the
    # blending is keyed off chat-state, not the tool name.
    origin_url = None
    if not has_image:
        try:
            from app.agents.origin_image import get_origin_url

            origin_url = get_origin_url(ctx.get("chat_id"))
        except Exception:  # noqa: BLE001
            pass

    try:
        if has_image:
            # Photo-pick: real resolved image drives the v6 query embedding.
            # text_query is informational only (v6 has no text param). NEVER an
            # LLM-supplied / placeholder URL.
            query = text_query or category or "fashion item"
            cands = await run_image_search(
                image_url=str(ctx_image),
                text_query=query,
                category=category,
                fit=fit,
                color_family=color_family,
                top_k=top_k,
            )
        elif origin_url:
            # Intent-aware Level 2 advanced blending (PR June 2026):
            # vector arithmetic for color_swap/fit_change, weighted-sum with
            # intent-tuned alpha otherwise. Falls through to text-only when
            # any step fails.
            # `prior_outfit_context` anchors `from_attribute` extraction — pass
            # the most recent product query so the classifier can spot the OLD
            # colour/fit token.
            prior_ctx_parts = [
                str(ctx.get("text_query") or ""),
                str(category or ""),
                str(fit or ""),
                str(color_family or ""),
            ]
            prior_ctx = " ".join(p for p in prior_ctx_parts if p).strip()
            cands = await run_smart_blended_search(
                origin_url=origin_url,
                modifier_query=text_query,
                chat_id=ctx.get("chat_id"),
                prior_outfit_context=prior_ctx or None,
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
        # P1-6 (260521): surface HTTP status (+host in log) so Modal cold-start /
        # PostgREST 5xx / Langfuse 4xx are distinguishable. Host is log-only
        # (internal infra — security 260522). Shared helper avoids duplication.
        logger.warning(
            "[tool.search_products] pipeline raised: %s (%r)",
            pipeline_exc_detail(exc, include_host=True),
            exc,
        )
        return SearchProductsResult(
            ok=False,
            error=f"pipeline_failed:{pipeline_exc_detail(exc, include_host=False)}",
            candidates_count=0,
            top_candidates=[],
        )

    # SPEC-AGENT-V3-REACT Gap4 — merge cross-thread dislike before persisting
    # (flag-gated; OFF → cands unchanged → V2 byte-identical).
    cands = apply_dislike_discount(ctx, cands)

    # User-supplied price bounds (KRW, integer 원). Applied AFTER vector
    # ranking + dislike discount so cosine ordering is preserved.
    cands = apply_price_filter(cands, args.get("min_price"), args.get("max_price"))

    # Persist FULL candidates for the turn so `respond` can render real cards
    # internally (the LLM never hand-serializes cards). LLM context still gets
    # only the small `top_candidates` summary below.
    persist_last_results(ctx, cands)

    # 260611 — emit `search_done` so subsequent turns' memory context surfaces
    # the prior query (drives the LLM toward `refine_search` instead of a fresh
    # `search_products` or a confused `get_recent_history`).
    emit_search_done(
        ctx=ctx,
        text_query=text_query,
        cands=cands,
        category=category,
        is_refine=False,
    )

    top = [_candidate_to_dict(c) for c in cands[:5]]
    return SearchProductsResult(ok=True, error=None, candidates_count=len(cands), top_candidates=top)
