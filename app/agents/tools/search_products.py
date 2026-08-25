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

import asyncio
import logging
import re
import time
from typing import Any

from app.agents.tool_registry import SearchProductsResult

logger = logging.getLogger(__name__)

# Mirrors refine_search._PINNED_PID_RE — mobile critique chips prepend
# `[#<id> · brand · name · ₩price]` to the user's text. When the LLM picks
# `search_products` (instead of `refine_search`) on such a turn — which can
# happen for broader "비슷한 거" intents — we anchor the search on the pinned
# product's image embedding, mirroring refine_search's behavior. Same fail-open
# semantics: any miss falls back to the legacy text path.
# Pattern anchored to `^\[#<digits>` (mobile prefix only) so free-text like
# "그 #1 같은 거" doesn't spuriously trigger a product_id fetch.
_PINNED_PID_RE = re.compile(r"^\[#(\d+)")

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
        distances: list[float] = []
        for c in cands or []:
            try:
                # id projection — cap at top 5 (LLM context shape).
                if len(top_ids) < 5:
                    pid = getattr(c, "id", None)
                    if pid is None and isinstance(c, dict):
                        pid = c.get("id")
                    if pid:
                        top_ids.append(str(pid))
                # distance projection — full set so the percentile is meaningful.
                d = getattr(c, "distance", None)
                if d is None and isinstance(c, dict):
                    d = c.get("distance")
                if d is not None:
                    distances.append(float(d))
            except Exception:  # noqa: BLE001 — best-effort projection
                continue
        # 260611 — distance stats (cosine, ASC=better). The pipeline already
        # logs min/median/max but the event payload didn't carry them, so beta
        # analysis SQL couldn't surface "how strong was this match?" Adding
        # them here makes `event_type='search_done'` rows self-sufficient for
        # search-quality dashboards (low p50 ≈ strong cluster match).
        dist_payload: dict[str, float] = {}
        if distances:
            dist_sorted = sorted(distances)
            dist_payload = {
                "min": round(dist_sorted[0], 4),
                "median": round(dist_sorted[len(dist_sorted) // 2], 4),
                "max": round(dist_sorted[-1], 4),
            }
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
                "distance": dist_payload,
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


# 브랜드 pivot("스킴스로 보여줘") 감지용 필러. 브랜드 이름 + gender + 아래
# 무의미 카테고리어만 남은 쿼리는 "스타일 서술이 없는" 브랜드 전환으로 본다.
# 이런 턴에서 LLM 이 이전 스타일 맥락을 버리고 맨몸 브랜드 검색을 날리면
# (닝닝 공항룩 → SKIMS → 언더웨어) 직전 성공검색의 스타일을 결정론적으로
# 이어붙여 "같은 무드, 다른 브랜드"를 유지한다. (2026-08-24)
_STYLELESS_FILLER: frozenset[str] = frozenset(
    {
        "top",
        "tops",
        "clothes",
        "clothing",
        "clothe",
        "items",
        "item",
        "fashion",
        "outfit",
        "outfits",
        "look",
        "looks",
        "style",
        "styles",
        "product",
        "products",
        "piece",
        "pieces",
        "thing",
        "things",
        "stuff",
        "wear",
        "apparel",
        "collection",
        "some",
        "any",
        "more",
        "show",
        "me",
    }
)


def _is_styleless_brand_query(text_query: str, brand_arg: Any, brand_filter: list[str] | None) -> bool:
    """`text_query` 가 브랜드/성별/무의미 필러만 담고 있으면 True.

    True 면 사용자가 브랜드만 바꿨을 뿐 새 스타일 서술을 주지 않은 것이므로,
    직전 검색의 스타일을 이어받아야 한다. 'nike running shoes women' 처럼
    실 스타일 토큰이 있으면 False (그건 진짜 새 검색).
    """
    residual = set(text_query.lower().split())
    residual -= set(_GENDER_TOKENS)
    residual -= _STYLELESS_FILLER
    # 브랜드 토큰 제거 (raw arg + resolve 된 canonical, 멀티워드 대응).
    for src in (str(brand_arg or ""), *(brand_filter or [])):
        for tok in src.lower().split():
            residual.discard(tok)
    return not residual


def _resolve_brand_filter(raw: Any) -> list[str] | None:
    """LLM `brand` arg → v6 `p_brand_names` 용 canonical 리스트 (2026-07-16).

    RPC 는 `brand_nodes.brand_name = ANY(p_brand_names)` EXACT 매치라 LLM
    표기("acne studios", "ACNE", "paf", "포스트아카이브팩션")를 그대로 보내면
    미스난다 — `brand_node_cache.resolve_brand_names` 로 canonical `brand_name`
    (들)을 resolve 한다. 한/영 표면형·괄호 약칭·이니셜 약칭을 모두 흡수하고,
    같은 브랜드의 중복 노드(예: 'Post Archive Faction' + '… (PAF)')는 모든
    canonical 명을 함께 반환해 RPC 가 두 노드에 걸린 상품을 다 잡는다.
    미인식/캐시 미워밍이면 None (fail-open: 필터 없이 진행)."""
    if not raw or not isinstance(raw, str) or not raw.strip():
        return None
    try:
        from app.infrastructure.repositories.brand_node_cache import resolve_brand_names

        names = resolve_brand_names(raw)
        if names:
            return names
        logger.info("[tool.search_products] brand %r not in brand_node_cache — filter skipped (fail-open)", raw)
    except Exception as exc:  # noqa: BLE001 — 브랜드 필터는 부가 기능, 검색을 막지 않는다
        logger.warning("[tool.search_products] brand resolve failed: %r", exc)
    return None


def _recover_pinned_brand(ctx: dict[str, Any]) -> list[str] | None:
    """Clarify 연속 턴에서 LLM 이 `brand` 를 빠뜨렸을 때 직전 검색의 브랜드를 유지.

    '글로니 제품 찾아줘 → (상의) → 다시 (바지)' 처럼 두 번째 clarify 답에서
    작은 모델이 브랜드를 놓쳐 다른 브랜드가 뜨던 문제 대응(프롬프트로 "keep
    brand" 지시해도 비결정적). 직전 턴 후보(`sess.last_results`)가 사실상 단일
    브랜드면 그 브랜드를 canonical 화해 필터로 재적용한다. 혼합 브랜드(일반
    검색 컨텍스트)면 None — 핀하지 않는다. best-effort, 절대 raise 안 함."""
    try:
        chat_id = ctx.get("chat_id")
        if chat_id is None:
            return None
        from app.infrastructure.memory.session import get_store

        sess = get_store().get_or_create(int(chat_id))
        cands = list(getattr(sess, "last_results", None) or [])
        brands: list[str] = []
        for c in cands:
            b = getattr(c, "brand", None)
            if b is None and isinstance(c, dict):
                b = c.get("brand")
            b = (b or "").strip()
            if b:
                brands.append(b)
        # 직전 검색 후보가 너무 적으면(무-검색 clarify 등) 추론 불가.
        if len(brands) < 3:
            return None
        from collections import Counter

        top_lower, top_n = Counter(b.lower() for b in brands).most_common(1)[0]
        # 사실상 단일 브랜드일 때만(≥80%) 핀 — 혼합이면 일반 검색이므로 건드리지 않는다.
        if top_n / len(brands) < 0.8:
            return None
        sample = next(b for b in brands if b.lower() == top_lower)
        return _resolve_brand_filter(sample)
    except Exception as exc:  # noqa: BLE001 — 핀은 부가 기능, 검색을 막지 않는다
        logger.debug("[tool.search_products] brand pin recover failed: %r", exc)
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


def effective_max_price(arg_max: Any, ctx: dict[str, Any]) -> Any:
    """Resolve the price ceiling: the LLM-supplied `max_price` arg wins; else
    fall back to the per-request mobile filter slider (`ctx['req_price_max']`).

    Shared by `search_products` and `refine_search` so both honor the mobile
    filter ceiling even when the LLM omits an explicit budget. Returns None when
    neither source provides a usable (>0) bound.
    """
    if arg_max is not None:
        return arg_max
    rp = ctx.get("req_price_max")
    try:
        return rp if rp and int(rp) > 0 else None
    except (TypeError, ValueError):
        return None


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


def _build_color_notice() -> str | None:
    """검색 파이프라인이 색 정밀 필터를 재고 부족으로 relax 했으면(요청 색이
    사실상 없어 유사상품으로 채운 경우), 에이전트가 사용자에게 정직하게 안내하도록
    self-instructing 관찰 문자열을 만든다. relax 없으면 None. 이 문자열은 그대로
    result.notice → react_loop result_summary 로 모델에 전달돼, "핑크로 바꿨어!"
    같은 거짓 확답(2026-08-24 실트레이스)을 막는다."""
    try:
        from app.services.search_service import color_relax_ctx

        meta = color_relax_ctx.get()
    except Exception:  # noqa: BLE001
        return None
    if not meta:
        return None
    color = meta.get("requested_color")
    if not color:
        return None
    exact = meta.get("exact_count")
    if meta.get("subcategory_also_relaxed"):
        return (
            f"scarce_match: few items exactly match the requested {color} for this category; "
            f"filled with close alternatives. Tell the user honestly that exact matches were "
            f"limited and these are similar picks — do NOT claim they are all {color}."
        )
    if exact == 0:
        return (
            f"no_exact_color: ZERO items match color={color} for this query; the results are "
            f"closest-style alternatives in OTHER colors. You MUST tell the user that {color} is "
            f"essentially unavailable for this style and that these are similar look-alikes "
            f"instead — do NOT claim the picks are {color}."
        )
    return (
        f"low_exact_color: only {exact} item(s) actually match color={color}; the rest are close "
        f"alternatives in other colors. Mention that exact-{color} stock is limited."
    )


def _cand_attr(c: Any, key: str) -> Any:
    """dict 또는 Candidate 객체 양쪽에서 필드 읽기."""
    if isinstance(c, dict):
        return c.get(key)
    return getattr(c, key, None)


async def _build_result_digest(cands: list[Any], *, limit: int = 15) -> dict[str, Any] | None:
    """결과셋의 속성 분포를 요약 — respond 가 "대부분 미디에 린넨" 처럼 구체적으로,
    그러나 사실에 근거해 묘사하도록(데이드림 벤치마크). 상위 `limit` 개의 subcategory/
    가격/브랜드(행에 직접 존재) + feature_metadata(fit/material/pattern/primary_color,
    없으면 1회 배치조회)를 집계. 명확한 우세값만 싣는다(혼재 축은 생략 → 지어내기·
    과일반화 방지). 아무 신호 없으면 None."""
    if not cands:
        return None
    sample = cands[:limit]

    # feature_metadata 확보 (색 relax 경로 등에서 이미 붙었으면 재사용, 없으면 배치).
    metas: dict[int, dict[str, Any]] = {}
    missing: list[int] = []
    for c in sample:
        try:
            pid = int(_cand_attr(c, "id") or _cand_attr(c, "product_id"))
        except (TypeError, ValueError):
            continue
        m = _cand_attr(c, "feature_metadata")
        if isinstance(m, dict):
            metas[pid] = m
        else:
            missing.append(pid)
    if missing:
        try:
            from app.providers import db_pool

            pool = db_pool._pool  # noqa: SLF001
            if pool is not None:
                async with pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(
                        "SELECT product_id, feature_metadata FROM public.product_features WHERE product_id = ANY(%s)",
                        (missing,),
                    )
                    for pid, meta in await cur.fetchall():
                        if isinstance(meta, dict):
                            metas[int(pid)] = meta
        except Exception:  # noqa: BLE001 — fail-open: digest degrades to row-only fields
            pass

    from collections import Counter

    n = len(sample)

    def _dominant(counter: Counter, *, min_share: float) -> str | None:
        """최빈값이 min_share 이상 점유할 때만 반환(혼재 축은 None)."""
        if not counter:
            return None
        val, cnt = counter.most_common(1)[0]
        return val if (cnt / n) >= min_share else None

    def _top_multi(counter: Counter, *, min_share: float, k: int) -> list[str]:
        return [v for v, cnt in counter.most_common(k) if (cnt / n) >= min_share]

    subcat_c: Counter = Counter()
    fit_c: Counter = Counter()
    pattern_c: Counter = Counter()
    color_c: Counter = Counter()
    material_c: Counter = Counter()
    brand_c: Counter = Counter()
    prices: list[int] = []

    for c in sample:
        sub = str(_cand_attr(c, "subcategory") or "").strip().lower()
        brand = str(_cand_attr(c, "brand") or "").strip()
        try:
            p = int(_cand_attr(c, "price") or 0)
        except (TypeError, ValueError):
            p = 0
        if brand:
            brand_c[brand] += 1
        if p > 0:
            prices.append(p)
        try:
            pid = int(_cand_attr(c, "id") or _cand_attr(c, "product_id"))
        except (TypeError, ValueError):
            pid = None
        meta = metas.get(pid) if pid is not None else None
        # 종류: subcategory(행) 우선, 없으면 feature_metadata.item_type.
        if sub and sub not in ("n/a", "none"):
            subcat_c[sub] += 1
        elif meta:
            it = str(meta.get("item_type") or "").strip().lower()
            if it and it not in ("n/a", "none"):
                subcat_c[it] += 1
        if meta:
            fit = str(meta.get("fit") or "").strip().lower()
            if fit and fit not in ("n/a", "none"):
                fit_c[fit] += 1
            pat = str(meta.get("pattern") or "").strip().lower()
            if pat and pat not in ("n/a", "none"):
                pattern_c[pat] += 1
            col = str(meta.get("primary_color") or "").strip().lower()
            if col and col not in ("n/a", "none"):
                color_c[col] += 1
            mats = meta.get("material")
            for mt in mats if isinstance(mats, list) else [mats]:
                mt = str(mt or "").strip().lower()
                if mt and mt not in ("n/a", "none"):
                    material_c[mt] += 1

    digest: dict[str, Any] = {}
    mostly = _dominant(subcat_c, min_share=0.34)
    if mostly:
        digest["mostly"] = mostly
    fit = _dominant(fit_c, min_share=0.5)
    if fit:
        digest["fit"] = fit
    mats = _top_multi(material_c, min_share=0.25, k=2)
    if mats:
        digest["materials"] = mats
    cols = _top_multi(color_c, min_share=0.25, k=2)
    if cols:
        digest["colors"] = cols
    pattern = _dominant(pattern_c, min_share=0.6)
    if pattern and pattern != "solid":  # solid 은 무의미 신호라 생략
        digest["pattern"] = pattern
    if prices:
        lo, hi = min(prices), max(prices)
        digest["price_krw"] = f"{lo:,}" if lo == hi else f"{lo:,}–{hi:,}"
    if brand_c:
        top_brands = [b for b, _ in brand_c.most_common(3)]
        extra = len(brand_c) - len(top_brands)
        digest["brands"] = ", ".join(top_brands) + (f" +{extra}" if extra > 0 else "")

    return digest or None


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
    subcategory: str | None = None,
    gender: str | None = None,
    brand_filter: list[str] | None = None,
    fit: str | None = None,
    color_family: str | None = None,
    name_query: str | None = None,
    top_k: int = 40,
    style_node_primary: str | None = None,
    user_key: str | None = None,
    override_embedding: list[float] | None = None,
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
    from app.models.request import AnalyzedItem, RecommendRequest, StyleNode
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
        # 2026-07-15: Vision/호출자 subcategory 그대로 운반 — 정규화(canonical
        # vocab 매칭)는 search_service._resolve_precision_filters 단일 지점.
        subcategory=subcategory,
        fit=fit,
        color_family=color_family,
        name_query=name_query,
        search_query=text_query,
    )
    style_node = StyleNode(primary=style_node_primary) if style_node_primary else None
    req = RecommendRequest(
        item=item,
        gender=gender,
        brand_filter=brand_filter,
        image_url=_TEXT_ONLY_SENTINEL,
        final_limit=max(1, int(top_k)),
        style_node=style_node,
    )
    state = PipelineState(request=req, user_key=user_key)
    # SPEC-SEARCH-HYBRID-001: a pure text query (no image-vector anchor) routes
    # to the image⊕text blend RPC. `override_embedding` is a product IMAGE
    # vector (pinned-product anchor) — keep v6 image ranking for that case since
    # a text_embedding blend against an image query vector is cross-modal.
    state.hybrid_text = override_embedding is None
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
        "🔍 [text_search] embed text_query=%r category=%r fit=%r color_family=%r top_k=%d override_embedding=%s",
        text_query[:120],
        category,
        fit,
        color_family,
        max(1, int(top_k)),
        "yes" if override_embedding else "no",
    )
    # 260522 per-step timing — the text-only path was opaquely slow (live: a
    # single search_products took 29.7s with a Modal embed timeout+retry). Time
    # each stage so the bottleneck is attributable from logs alone:
    #   ⏱ embed  = Modal /embed/text (cold-start prone; cache hit ≈ 0ms)
    #   ⏱ rpc    = search_step (PostgREST RPC + family gate)
    #   ⏱ divers = diversify_step (brand/platform/content caps)
    # 260701: when the caller supplies `override_embedding` (e.g. refine_search
    # anchored on a pinned product's `public.product_embeddings.embedding` row),
    # skip the Modal text-embed call entirely — the product's image embedding
    # IS the query vector. text_query is still required (for logging + the
    # AnalyzedItem.search_query metadata path), but never reaches Modal.
    _t_embed0 = time.perf_counter()
    if override_embedding is not None:
        state.embedding = list(override_embedding)
    else:
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


def _cand_id(cand: Any) -> Any:
    """product_id 추출 (dict / Candidate 양쪽)."""
    if isinstance(cand, dict):
        return cand.get("id") or cand.get("product_id")
    return getattr(cand, "id", None) or getattr(cand, "product_id", None)


async def run_multi_query_search(
    *,
    queries: list[str],
    gender: str | None = None,
    brand_filter: list[str] | None = None,
    top_k: int = 40,
    style_node_primary: str | None = None,
    user_key: str | None = None,
) -> list[Any]:
    """상황/TPO 쿼리 확장 (2026-07-16 Phase 4a).

    "결혼식 하객룩" 같은 상황 쿼리는 단일 아이템으로 답할 수 없다 — LLM 이
    2~3개 구성 아이템 쿼리("elegant midi dress" / "satin blouse" /
    "slingback heels")로 확장하고, 여기서 각각을 **병렬** text 검색한 뒤
    **인터리브 병합**(라운드로빈)해 하나의 코디 믹스를 만든다. gender 는
    시스템이 모든 서브쿼리에 동일 적용(하드 필터) — 사용자가 여성 하객룩을
    물으면 아이템 전부 여성.

    각 서브쿼리는 자체 diversify(브랜드/플랫폼 캡)를 거친 뒤 병합되므로,
    믹스는 아이템 종류가 다양하면서도 브랜드가 한쪽으로 쏠리지 않는다.
    실패한 서브쿼리는 건너뛴다(부분 실패 허용)."""
    results = await asyncio.gather(
        *[
            run_text_only_search(
                text_query=q,
                gender=gender,
                brand_filter=brand_filter,
                top_k=top_k,
                style_node_primary=style_node_primary,
                user_key=user_key,
            )
            for q in queries
        ],
        return_exceptions=True,
    )
    lists: list[list[Any]] = []
    for q, r in zip(queries, results, strict=True):
        if isinstance(r, list):
            lists.append(r)
        else:
            logger.warning("[multi_query] sub-query %r failed: %r", q[:60], r)
    # 라운드로빈 인터리브 + id dedup — top_k 까지.
    merged: list[Any] = []
    seen: set[Any] = set()
    depth = max((len(lst) for lst in lists), default=0)
    for i in range(depth):
        for lst in lists:
            if i < len(lst):
                cand = lst[i]
                cid = _cand_id(cand)
                if cid is not None and cid in seen:
                    continue
                seen.add(cid)
                merged.append(cand)
                if len(merged) >= top_k:
                    logger.info(
                        "🔍 [multi_query] %d subq → %d merged (interleaved, top_k=%d)",
                        len(lists),
                        len(merged),
                        top_k,
                    )
                    return merged
    logger.info("🔍 [multi_query] %d subq → %d merged (interleaved)", len(lists), len(merged))
    return merged


async def run_blended_search(
    *,
    origin_url: str,
    modifier_query: str,
    chat_id: int | None = None,
    alpha: float = 0.7,
    category: str | None = None,
    subcategory: str | None = None,
    gender: str | None = None,
    brand_filter: list[str] | None = None,
    fit: str | None = None,
    color_family: str | None = None,
    top_k: int = 40,
    style_node_primary: str | None = None,
    user_key: str | None = None,
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
    from app.models.request import AnalyzedItem, RecommendRequest, StyleNode
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
            subcategory=subcategory,
            gender=gender,
            brand_filter=brand_filter,
            fit=fit,
            color_family=color_family,
            top_k=top_k,
            style_node_primary=style_node_primary,
            user_key=user_key,
        )

    modifier_vec = await EmbedProvider.embed_text(modifier_query)
    _embed_ms = int((time.perf_counter() - _t0) * 1000)

    blended = blend_vectors(origin_vec, modifier_vec, alpha)

    item = AnalyzedItem(
        id="agent-blended",
        category=category or "apparel",
        subcategory=subcategory,
        fit=fit,
        color_family=color_family,
        search_query=modifier_query,
    )
    style_node = StyleNode(primary=style_node_primary) if style_node_primary else None
    req = RecommendRequest(
        item=item,
        gender=gender,
        brand_filter=brand_filter,
        image_url=_TEXT_ONLY_SENTINEL,
        final_limit=max(1, int(top_k)),
        style_node=style_node,
    )
    state = PipelineState(request=req, user_key=user_key)
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
    subcategory: str | None = None,
    gender: str | None = None,
    brand_filter: list[str] | None = None,
    fit: str | None = None,
    color_family: str | None = None,
    top_k: int = 40,
    style_node_primary: str | None = None,
    user_key: str | None = None,
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
    from app.models.request import AnalyzedItem, RecommendRequest, StyleNode
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
            subcategory=subcategory,
            gender=gender,
            brand_filter=brand_filter,
            fit=fit,
            color_family=color_family,
            top_k=top_k,
            style_node_primary=style_node_primary,
            user_key=user_key,
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
        subcategory=subcategory,
        fit=fit,
        color_family=color_family,
        search_query=modifier_query,
    )
    style_node = StyleNode(primary=style_node_primary) if style_node_primary else None
    req = RecommendRequest(
        item=item,
        gender=gender,
        brand_filter=brand_filter,
        image_url=_TEXT_ONLY_SENTINEL,
        final_limit=max(1, int(top_k)),
        style_node=style_node,
    )
    state = PipelineState(request=req, user_key=user_key)
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
    subcategory: str | None = None,
    gender: str | None = None,
    brand_filter: list[str] | None = None,
    fit: str | None = None,
    color_family: str | None = None,
    top_k: int = 40,
    style_node_primary: str | None = None,
    user_key: str | None = None,
) -> list[Any]:
    """Photo-pick path — full existing `run_pipeline` (image embedding → v6 RPC).

    `image_url` MUST be an externally-resolved URL sourced from ctx (never an
    LLM arg, never a placeholder).
    """
    from app.models.request import AnalyzedItem, RecommendRequest, StyleNode
    from app.pipeline.runner import run_pipeline

    # SPEC-SEARCH-V6-001: `category` is the REAL Vision garment category
    # (ctx.vision_category). It flows into AnalyzedItem → search_service →
    # build_params → to_canonical_family (the canonical 20-family gate). The
    # "apparel" fallback only applies when no Vision item is present; it
    # normalizes to `other` (gate skipped).
    item = AnalyzedItem(
        id="agent-v2",
        category=category or "apparel",
        subcategory=subcategory,
        fit=fit,
        color_family=color_family,
        search_query=text_query,
    )
    style_node = StyleNode(primary=style_node_primary) if style_node_primary else None
    req = RecommendRequest(
        item=item,
        gender=gender,
        brand_filter=brand_filter,
        image_url=image_url,
        final_limit=max(1, int(top_k)),
        style_node=style_node,
    )
    resp = await run_pipeline(req, user_key=user_key)
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

    # 260701 — Pinned-product anchor (mirrors refine_search). When the user's
    # CURRENT message text (ctx.text_query is the RAW user message before the
    # LLM's English-translated args.text_query) carries a `#<id>` prefix from
    # a mobile pinned card, anchor the search on that product's image
    # embedding instead of re-embedding text. Only fires on the text-only
    # path (no fresh image, no origin_url blend) — image / blended paths
    # already have their own image anchor. fail-open on any error/miss.
    pinned_embedding: list[float] | None = None
    pinned_category: str | None = None
    pinned_pid: int | None = None
    raw_msg = ctx.get("text_query") or ""
    _pid_match = _PINNED_PID_RE.search(raw_msg) if isinstance(raw_msg, str) else None
    if _pid_match is not None and not has_image:
        try:
            pinned_pid = int(_pid_match.group(1))
        except (TypeError, ValueError):
            pinned_pid = None
    if pinned_pid is not None:
        try:
            from app.providers.database import DatabaseProvider

            pinned_embedding = await DatabaseProvider.get_product_embedding(pinned_pid)
            pinned_category = await DatabaseProvider.get_product_category(pinned_pid)
        except Exception:  # noqa: BLE001 — fail-open to text path
            pinned_embedding = None
            pinned_category = None
        if pinned_embedding is not None:
            logger.info(
                "🔍 [tool.search_products] anchored on pinned product_id=%s (dim=%d category=%r)",
                pinned_pid,
                len(pinned_embedding),
                pinned_category,
            )
        else:
            logger.info(
                "🔍 [tool.search_products] #%s in message but embedding unavailable — falling back to text path",
                pinned_pid,
            )

    # B22 — wire `boost_keywords` / `exclude_keywords` into the dispatch.
    # Both fields are advertised in the SearchProductsArgs schema since the
    # 2026-05-16 V2 ReAct rollout (e21f746) but `search_products` never
    # actually read them — silently dropping the LLM's signal. Beta trace
    # 499840bb (09:22 73e5c867): LLM correctly extracted boost=["stomper
    # chunky heavy"] from the user's "발렌시아가 스톰퍼" reference, and the
    # dispatch threw it away → the embedding lost the brand cue entirely and
    # results felt generic. Now the boost is merged into text_query using
    # the same dedup-join helper `refine_search` uses (B15) so chained
    # signals don't accumulate the same token.
    from app.agents.tools._keyword_utils import as_keyword_list, dedup_join

    boost = as_keyword_list(args.get("boost_keywords"))
    if boost:
        text_query = dedup_join(text_query, boost)
    exclude_kw = as_keyword_list(args.get("exclude_keywords"))

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
            # Priority: gender word the user typed THIS turn (explicit_gender,
            # handled above) > per-request mobile filter (ctx.req_gender) >
            # pinned taste profile. The filter is a deliberate UI choice, so it
            # overrides the profile pin — but it is per-request only and never
            # persisted (SPEC-GENDER-PIN-001).
            req_gender = ctx.get("req_gender")
            pinned = req_gender if req_gender in _GENDER_TOKENS else _lookup_profile_gender(ctx)
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
        # 260701 — Skip persistence on anchor turns (mirrors refine_search
        # cleanup PR #113). args.text_query on an anchor turn is the LLM's
        # interpretation of "[#id · brand · ...] chip" and would pollute
        # base_query on subsequent legacy refines.
        if pinned_embedding is None:
            try:
                from app.agents.last_query import push_recent_search, set_last_query

                set_last_query(ctx.get("chat_id"), text_query)
                # P2 (2026-08-24) — 링버퍼에도 기록해 "다시 <화제>" 되부름 앵커 확보.
                # label 은 사용자 원문(raw_msg, 영어 번역 전). brand 는 아래에서
                # 확정되므로 여기선 label+q 만; brand-pivot 병합 블록이 갱신한다.
                push_recent_search(ctx.get("chat_id"), text_query, label=str(raw_msg or ""))
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
    # 260701 — Pinned anchor overrides category and (since args.fit / args.color
    # already win the legacy path) does nothing extra to fit/color here.
    # ctx.vision_category leak: prior knit turn → pinned jeans → search would
    # still apply the knit family gate. Mirrors refine_search PR #112.
    if pinned_embedding is not None:
        category = pinned_category or ctx.get("vision_category")
        # Pinned anchor: 이전 Vision 턴의 subcategory 를 새 anchor 에 누출하지
        # 않는다 (fit/color 클리어와 동일한 원칙 — refine_search PR #112).
        subcategory = None
    else:
        # 2026-07-15 배선 수정: 순수 텍스트 턴은 vision_category 가 None 이라
        # LLM `category` arg 가 family gate 에 전혀 닿지 않았다 (gender 재검색
        # 경로만 args 를 stash 하는 비대칭). Vision 우선, 없으면 args 로 폴백 —
        # args 는 자유형("hoodie")이어도 to_canonical_family / subcategory_vocab
        # 이 정규화하고, 미인식 값은 `other`/None 으로 fail-open.
        category = ctx.get("vision_category") or args.get("category")
        subcategory = ctx.get("vision_subcategory")
    fit = args.get("fit")
    color_family = args.get("color_family")
    # 특정 상품/모델 지목 시 상품명 trigram 매칭어 (예: '2021M', 'trompe l’oeil').
    name_query = str(args.get("name_query") or "").strip() or None

    # 2026-07-16 — 구조화 gender (v6 p_gender 하드 필터). 위의 gender
    # resolution 블록이 모든 경로에서 최종 토큰을 text_query 에 남기므로
    # (명시/핀 append/unisex 폴백), 거기서 역파싱하는 것이 단일 소스다.
    # 'unisex' 는 search_service._resolve_gender 가 None(필터 off)으로 매핑.
    structured_gender = _query_gender(text_query)

    # 2026-07-16 — 브랜드 지정 요청 (LLM `brand` arg): brand_node_cache 로
    # canonical brand_name 을 resolve 해서 p_brand_names EXACT 매치에 태운다.
    # 미인식 브랜드는 필터 없이 진행 (fail-open — 브랜드 토큰은 text_query
    # 임베딩에 그대로 남아 soft 신호로 작동).
    brand_filter = _resolve_brand_filter(args.get("brand"))

    # 브랜드 sticky 핀(결정론적): 에이전트가 낯선 국내 브랜드('글로니')를 brand
    # arg 로 안 넣는 문제 보정. react_loop._build_ctx 가 원문에서 브랜드를 감지해
    # 세션 핀에 저장하므로, LLM 이 brand 를 빠뜨렸으면 그 핀을 적용한다
    # ('글로니 → (상의) → (바지)' 전 구간 유지). 핀이 없고 clarify 답 턴이면
    # 직전 검색 결과(단일 브랜드)에서 복구도 시도(보조).
    if brand_filter is None:
        from app.agents.last_query import get_pinned_brand

        pinned = get_pinned_brand(ctx.get("chat_id"))
        if not pinned and ctx.get("from_clarify_answer"):
            pinned = _recover_pinned_brand(ctx)
        if pinned:
            brand_filter = pinned
            logger.info("[tool.search_products] brand pin: applied %r (agent omitted brand)", pinned)

    # 브랜드 pivot style carry (2026-08-24): 브랜드는 지정됐는데 text_query 가
    # 스타일 서술 없이 브랜드/성별/필러뿐이면("스킴스로 보여줘"), 직전 성공검색의
    # 스타일을 이어붙인다. 없으면 SKIMS 처럼 카탈로그 기본이 언더웨어인 브랜드에서
    # 맨몸 검색이 무드를 통째로 잃는다 (닝닝 공항룩 → 팬티/브라 실트레이스).
    # anchor 턴(pinned_embedding)은 자체 이미지 앵커가 있으니 제외.
    if (
        brand_filter
        and pinned_embedding is None
        and _is_styleless_brand_query(text_query, args.get("brand"), brand_filter)
    ):
        try:
            from app.agents.last_query import get_last_query

            prior_style = get_last_query(ctx.get("chat_id")) or ""
        except Exception:  # noqa: BLE001
            prior_style = ""
        if prior_style:
            merged = dedup_join(text_query, [prior_style])
            if merged and merged != text_query:
                logger.info(
                    "[tool.search_products] brand-pivot style carry: %r + prior %r → %r",
                    text_query,
                    prior_style,
                    merged,
                )
                text_query = merged
                structured_gender = _query_gender(text_query)
                ctx["text_query"] = text_query
                try:
                    from app.agents.last_query import set_last_query

                    set_last_query(ctx.get("chat_id"), text_query)
                except Exception:  # noqa: BLE001
                    pass

    # P2 (2026-08-24) — 링버퍼 최종 갱신: brand 확정 + (pivot 시) 병합된 text_query 를
    # 반영. label 이 같으므로 main persist 에서 만든 엔트리를 in-place 갱신한다.
    if pinned_embedding is None and text_query:
        try:
            from app.agents.last_query import push_recent_search

            push_recent_search(ctx.get("chat_id"), text_query, brand=brand_filter, label=str(raw_msg or ""))
        except Exception:  # noqa: BLE001
            pass

    # 브랜드 지정 검색은 "그 브랜드 상품을 최대한 다 보여줘"가 의도다. 기본
    # top_k(15)로는 한 브랜드만 볼 때 너무 적으니, LLM 이 더 큰 값을 주지 않은
    # 한 앨범 한 페이지(스트리밍 album_size=40)에 맞춰 상향한다. diversify 는
    # brand_filter 활성 시 다양성 캡을 끄므로 이 만큼 실제로 채워진다.
    if brand_filter:
        top_k = max(top_k, 40)

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

    # SPEC-SEARCH-V6-STYLE-WIRING — Vision-derived style letter (A..U) that
    # react_loop._build_ctx parks in ctx (sourced from
    # state.vision_outfit_style_node_primary). Forwarded to every search
    # path so v6's FILTER1 rung-1 (`p_style_node_id` EXACT + family) can
    # engage instead of the previous always-degraded baseline. None → RPC
    # falls back to rung-2 cleanly.
    # Args > ctx > None. text-only turns have no Vision letter in ctx, so the
    # LLM-supplied `args.style_node_primary` (chosen from the 21-letter digest
    # in the tool description) is what activates v6 rung-1 on those turns.
    # Vision turns still benefit: when the LLM also supplies a letter, args
    # wins, but if it omits the field, ctx (Vision) takes over.
    _args_sn = args.get("style_node_primary")
    if pinned_embedding is not None:
        # Pinned anchor: ignore ctx.style_node_primary (prior turn's brand letter).
        # Only respect an explicit LLM override.
        style_node_primary = _args_sn if (isinstance(_args_sn, str) and _args_sn.strip()) else None
    else:
        style_node_primary = (
            _args_sn if (isinstance(_args_sn, str) and _args_sn.strip()) else ctx.get("style_node_primary")
        )
    # SPEC-PERSONALIZE-RERANK — forward the per-turn user_key so search_service
    # can look up TasteProfile and re-order the v6 raw rows.
    user_key = ctx.get("user_key")

    # 2026-07-16 Phase 4a — 상황/TPO 쿼리 확장 (멀티 쿼리). LLM 이 상황
    # 쿼리에서만 sub_queries 를 채운다. 순수 텍스트 턴에서만 활성 (이미지/핀
    # 앵커 턴은 단일 아이템 의도라 제외). text_query(대표) + sub_queries 를
    # dedup 해 2개 이상이면 멀티 경로. structured_gender 는 모든 서브쿼리에
    # 동일 적용된다.
    multi_queries: list[str] | None = None
    _sub_raw = args.get("sub_queries")
    if not has_image and pinned_embedding is None and isinstance(_sub_raw, list) and _sub_raw:
        _seen_q: set[str] = set()
        _mq: list[str] = []
        for q in [text_query, *_sub_raw]:
            if not isinstance(q, str):
                continue
            qs = q.strip()
            if qs and qs.lower() not in _seen_q:
                _seen_q.add(qs.lower())
                _mq.append(qs)
        if len(_mq) >= 2:
            multi_queries = _mq[:3]  # 레이턴시 상한 — 최대 3개 병렬 검색

    try:
        if multi_queries is not None:
            logger.info("🔍 [tool.search_products] situation multi-query: %r", multi_queries)
            cands = await run_multi_query_search(
                queries=multi_queries,
                gender=structured_gender,
                brand_filter=brand_filter,
                top_k=top_k,
                style_node_primary=style_node_primary,
                user_key=user_key,
            )
        elif has_image:
            # Photo-pick: real resolved image drives the v6 query embedding.
            # text_query is informational only (v6 has no text param). NEVER an
            # LLM-supplied / placeholder URL.
            query = text_query or category or "fashion item"
            cands = await run_image_search(
                image_url=str(ctx_image),
                text_query=query,
                category=category,
                subcategory=subcategory,
                gender=structured_gender,
                brand_filter=brand_filter,
                fit=fit,
                color_family=color_family,
                top_k=top_k,
                style_node_primary=style_node_primary,
                user_key=user_key,
            )
        elif origin_url and pinned_embedding is None:
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
                subcategory=subcategory,
                gender=structured_gender,
                brand_filter=brand_filter,
                fit=fit,
                color_family=color_family,
                top_k=top_k,
                style_node_primary=style_node_primary,
                user_key=user_key,
            )
        else:
            cands = await run_text_only_search(
                text_query=text_query,
                category=category,
                subcategory=subcategory,
                gender=structured_gender,
                brand_filter=brand_filter,
                fit=fit,
                color_family=color_family,
                name_query=name_query,
                top_k=top_k,
                style_node_primary=style_node_primary,
                user_key=user_key,
                override_embedding=pinned_embedding,
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

    # B22 — `exclude_keywords` (LLM-supplied) thin client-side filter, parity
    # with refine_search. Drops candidates whose `title` or `name` contains
    # any excluded token (case-insensitive). Handles both Candidate objects
    # and raw dicts so it works across the pipeline shapes.
    if exclude_kw:
        ek = {k.lower() for k in exclude_kw}

        def _title_of(c: Any) -> str:
            if isinstance(c, dict):
                return str(c.get("title") or c.get("name") or "")
            return str(getattr(c, "title", None) or getattr(c, "name", "") or "")

        cands = [c for c in cands if not any(k in _title_of(c).lower() for k in ek)]

    # User-supplied price bounds (KRW, integer 원). Applied AFTER vector
    # ranking + dislike discount so cosine ordering is preserved. The ceiling
    # falls back to the per-request mobile filter slider when the LLM didn't
    # supply an explicit max_price.
    cands = apply_price_filter(cands, args.get("min_price"), effective_max_price(args.get("max_price"), ctx))

    # Persist FULL candidates for the turn so `respond` can render real cards
    # internally (the LLM never hand-serializes cards). LLM context still gets
    # only the small `top_candidates` summary below.
    persist_last_results(ctx, cands)

    # 2026-08-19 — 직전 브랜드 필터 보관 → refine("다른 색상으로")이 브랜드를
    # 유지한다. 브랜드 없는 검색이면 set_last_brand(None) 이 이전 값을 지워 stale
    # 브랜드가 새지 않게 한다. pinned anchor 턴은 last_query 와 동일하게 제외.
    if pinned_embedding is None:
        try:
            from app.agents.last_query import set_last_brand

            set_last_brand(ctx.get("chat_id"), brand_filter)
        except Exception:  # noqa: BLE001
            pass

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
    result = SearchProductsResult(ok=True, error=None, candidates_count=len(cands), top_candidates=top)
    _notice = _build_color_notice()
    if _notice:
        result["notice"] = _notice
    _digest = await _build_result_digest(cands)
    if _digest:
        result["digest"] = _digest
    return result
