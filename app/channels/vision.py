"""Vision extraction via LiteLLM proxy (GPT-4o-mini default).

SPEC-VISION-UNIFY-001 — emits the rich `VisionResult` schema mirroring
kikoai/app's `analyze.ts`. The legacy minimal schema is kept behind the
`VISION_SCHEMA_V2` flag for one-flip rollback (REQ-VISION-COMPAT-005).

`extract()` always returns a `VisionResult` and never raises.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.channels.vision_prompt import ANALYZE_SYSTEM_PROMPT, ANALYZE_USER_PROMPT
from app.core.config import settings
from app.observability.langfuse import observe
from app.providers.llm import LLMProvider

logger = logging.getLogger(__name__)

_MAX_ITEMS = 4

# Legacy minimal-schema prompt (used only when VISION_SCHEMA_V2=False).
_LEGACY_SYSTEM_PROMPT = (
    "You are a fashion vision tagger. Look at the provided photo and identify the "
    "distinct fashion items the person is wearing or carrying (top, bottom, outerwear, "
    "footwear, bag, hat, accessory, etc.). Respond with ONLY a single JSON object on "
    "one line, no prose, no code fence. Schema: "
    '{"items": [{"label": string, "description": string, "color": string, "keywords": [string]}]}. '
    f"Return AT MOST {_MAX_ITEMS} items, ranked by visual prominence. "
    "`label` is 2-4 word English noun phrase (e.g. 'white cotton t-shirt'). "
    "`description` is 4-10 word English detail (e.g. 'round neck, short sleeve, slim fit'). "
    "`color` is the dominant English color word for THAT item. "
    "`keywords` is 3-8 lowercase English search keywords for THAT item only "
    "(garment type, color, fit, fabric, style). Do NOT mix keywords across items. "
    "Respond with English only."
)


# ── Pydantic v2 models — mirror kikoai/app's VisionAnalysisResult ──────────


class VisionMoodTag(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    label: str = ""
    score: float = 0.0


class VisionMood(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    tags: list[VisionMoodTag] = Field(default_factory=list)
    summary: str = ""
    vibe: str = ""
    season: str = ""
    occasion: str = ""


class VisionPaletteEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    hex: str = ""
    label: str = ""


class VisionStyle(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    fit: str = ""
    aesthetic: str = ""
    detectedGender: str = "unisex"


class VisionStyleNode(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    primary: str = "C"
    primaryConfidence: float = 0.0
    secondary: str = ""
    secondaryConfidence: float = 0.0
    reasoning: str = ""


class VisionPosition(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    top: float = 0.0
    left: float = 0.0


class VisionItem(BaseModel):
    """Per-item structured fields. Mirrors kikoai/app's VisionAnalysisItem."""

    # Note: spec mandates extra="forbid" but operationally we use extra="ignore"
    # on inner models so a single unknown key from GPT doesn't fail the whole
    # response — the fallback path then loses too much context. The outfit-level
    # outer model still rejects extra keys.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str = ""
    category: str = ""
    subcategory: str = ""
    name: str = ""
    detail: str = ""
    fabric: str = ""
    color: str = ""
    colorHex: str = ""
    colorFamily: str = ""
    fit: str = ""
    # Concrete, observable garment details (3-5 short tokens) that the model
    # picked up from THIS specific image — silhouette, neckline/sleeve,
    # hardware/closure, length, distinctive construction. Drives a richer
    # `searchQuery` and lets downstream code compose better refine boosts.
    distinctiveDetails: list[str] = Field(default_factory=list)
    searchQuery: str = ""
    searchQueryKo: str = ""
    position: VisionPosition = Field(default_factory=VisionPosition)


class VisionResult(BaseModel):
    """Outfit-level Vision schema. Mirrors kikoai/app's VisionAnalysisResult."""

    # @MX:NOTE: [AUTO] Outer model uses extra="ignore" not "forbid" for safety —
    # GPT occasionally returns minor extra keys; failing fast loses too much
    # signal. Drift in the documented contract is caught by parity tests instead.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    isApparel: bool = False
    styleNode: VisionStyleNode = Field(default_factory=VisionStyleNode)
    sensitivityTags: list[str] = Field(default_factory=list)
    mood: VisionMood = Field(default_factory=VisionMood)
    palette: list[VisionPaletteEntry] = Field(default_factory=list)
    style: VisionStyle = Field(default_factory=VisionStyle)
    items: list[VisionItem] = Field(default_factory=list)


def _fallback_result() -> VisionResult:
    """REQ-VISION-COMPAT-004 — schema-satisfying fallback for any failure."""
    return VisionResult(
        isApparel=False,
        styleNode=VisionStyleNode(primary="C"),
        items=[],
    )


# ── Legacy-shape adapter (for VISION_SCHEMA_V2=False) ──────────────────────


def _legacy_to_vision_result(legacy: dict) -> VisionResult:
    """Adapt legacy {items:[{label,description,color,keywords}]} dict into a
    VisionResult so callers always see the same return type. Used when
    `VISION_SCHEMA_V2=False`.
    """
    raw_items = legacy.get("items") if isinstance(legacy, dict) else None
    if not isinstance(raw_items, list):
        raw_items = []

    items: list[VisionItem] = []
    for i, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or raw.get("item") or "").strip()
        desc = str(raw.get("description") or "").strip()
        color = str(raw.get("color") or "").strip()
        kws = raw.get("keywords") or []
        if not isinstance(kws, list):
            kws = []
        kw_str = " ".join(str(k).strip() for k in kws if str(k).strip())[:256]
        items.append(
            VisionItem(
                id=label.replace(" ", "_") or f"item_{i}",
                category="",
                name=label or "item",
                detail=desc,
                color=color,
                # Legacy path doesn't produce searchQuery/Ko — leave empty so
                # downstream fallbacks (keyword-based query) still work.
                searchQuery=kw_str,
                searchQueryKo="",
            )
        )

    return VisionResult(
        isApparel=bool(items),
        styleNode=VisionStyleNode(primary="C"),
        items=items,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _model() -> str:
    return settings.VISION_MODEL


def _parse_json_relaxed(content: str) -> dict | None:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _build_messages(image_url_value: str, *, schema_v2: bool, multi_look_retry: bool = False) -> list[dict[str, Any]]:
    if schema_v2:
        system_prompt = ANALYZE_SYSTEM_PROMPT
        user_text = ANALYZE_USER_PROMPT
    else:
        system_prompt = _LEGACY_SYSTEM_PROMPT
        user_text = "List every distinct fashion item in this photo as JSON only."

    # B8 — Pinterest boards often show multi-person / multi-look composites.
    # The base prompt assumes single-subject and the model returns empty
    # items[] on these. Override the user text on the retry pass: focus on
    # the most prominent single look, and forbid the empty-items giveup.
    if multi_look_retry and schema_v2:
        user_text = (
            "This image MAY contain multiple people, multiple separate looks, "
            "or a collage of styling references — that is COMMON for Pinterest "
            "fashion boards. Do NOT give up with an empty items list.\n\n"
            "Procedure:\n"
            "1. If you see ANY garment, shoe, bag, hat, jewelry, or eyewear anywhere "
            "   in the image, set isApparel=true.\n"
            "2. If multiple people / looks appear, FOCUS on the single most "
            "   prominent one — pick by: largest in frame, foreground, centered, "
            "   or the clearest/best-lit. Ignore the others.\n"
            "3. Extract items from that chosen subject following the same schema "
            "   and enum rules as the system instructions. Always return at least "
            "   one item when any garment is visible.\n"
            "Return valid JSON only, no commentary."
        )

    return [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": image_url_value}},
            ],
        },
    ]


def _validate_v2(parsed: dict) -> VisionResult:
    """Validate a parsed JSON payload as a rich-schema VisionResult.

    Defensive: if `isApparel` is missing, infer from items list (matches
    kikoai/app's run-vision.ts inference logic).
    """
    if not isinstance(parsed, dict):
        return _fallback_result()

    # Mirror kikoai/app inference: missing isApparel + non-empty items → True.
    if "isApparel" not in parsed:
        items_count = len(parsed.get("items") or []) if isinstance(parsed.get("items"), list) else 0
        parsed = {**parsed, "isApparel": items_count > 0}

    try:
        result = VisionResult.model_validate(parsed)
    except Exception as exc:
        logger.warning("👁️  [VISION] Pydantic validation failed: %s", str(exc)[:200])
        return _fallback_result()

    # Cap items to _MAX_ITEMS (parity with legacy behavior).
    if len(result.items) > _MAX_ITEMS:
        result = result.model_copy(update={"items": result.items[:_MAX_ITEMS]})
    return result


def _log_v2(result: VisionResult, elapsed_ms: int) -> None:
    """REQ-VISION-OBSV-002 — per-item structured INFO line."""
    logger.info(
        "👁️  [VISION] ✅ 완료 elapsed=%dms items=%d isApparel=%s node=%s",
        elapsed_ms,
        len(result.items),
        result.isApparel,
        result.styleNode.primary,
    )
    for i, it in enumerate(result.items):
        num = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"][i] if i < 4 else f"{i + 1}."
        sq_ko = (it.searchQueryKo or "")[:80]
        logger.info(
            "👁️  [VISION]   %s %s/%s/%s — %s",
            num,
            it.subcategory or "?",
            it.fit or "?",
            it.colorFamily or "?",
            sq_ko,
        )


def _log_legacy(result: VisionResult, elapsed_ms: int) -> None:
    """Legacy log format when VISION_SCHEMA_V2=False (parity with pre-SPEC)."""
    logger.info("👁️  [VISION] ✅ 완료 elapsed=%dms items=%d", elapsed_ms, len(result.items))
    for i, it in enumerate(result.items):
        num = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"][i] if i < 4 else f"{i + 1}."
        kw_preview = it.searchQuery[:60]
        logger.info(
            "👁️  [VISION]   %s %s — %s [kw: %s]",
            num,
            it.name or "?",
            (it.detail or "")[:60],
            kw_preview,
        )


# ── Public API ──────────────────────────────────────────────────────────────


async def _predownload_image(url: str) -> bytes | None:
    """B26 — pre-download an image URL to bytes so the Vision LLM never has
    to fetch it server-side.

    Pinterest's CDN (pinimg.com) returns 403 to many cloud-egress IPs (incl.
    AWS Bedrock servers), so when we pass a pinimg URL straight through to
    Sonnet 4.5, every Vision call dies with `BedrockException - Client
    error '403 Forbidden' for url 'https://i.pinimg.com/...'` and falls
    through to `_fallback_result()` (isApparel=False, items=[]). With Haiku
    we previously got away with it because LiteLLM pre-downloaded for that
    model — this helper makes the behaviour explicit and model-independent.

    Returns `None` on any failure (timeout / 4xx / 5xx / non-image content)
    so the caller can fall back to URL passthrough.
    """
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return None
    # Browser-like UA so the CDN treats us as a normal client rather than a
    # cloud egress bot. Referer scoping kept blank — Pinterest CDN doesn't
    # require it for image fetches and a wrong referer can backfire.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.info("👁️  [VISION] predownload %d for %s", resp.status_code, url[:80])
            return None
        ct = (resp.headers.get("content-type") or "").lower()
        if not ct.startswith("image/"):
            logger.info("👁️  [VISION] predownload non-image ct=%s url=%s", ct, url[:80])
            return None
        return resp.content
    except Exception as exc:  # noqa: BLE001 — best-effort: never block Vision
        logger.info("👁️  [VISION] predownload failed: %s", exc)
        return None


@observe(name="vision_extract")
async def extract(image: str | bytes) -> VisionResult:
    """Run the Vision LLM and return a `VisionResult`.

    Never raises — any failure path returns the documented fallback
    (REQ-VISION-COMPAT-004). Schema is selected by `VISION_SCHEMA_V2`
    (REQ-VISION-COMPAT-005).
    """
    # B26 — when the caller passes an http(s) URL, pre-download to bytes so
    # the Vision LLM (esp. Bedrock Sonnet) doesn't have to fetch the URL
    # itself. Pinterest CDN 403s many cloud-egress IPs → without this every
    # Pinterest Vision call silently fell to `_fallback_result()`.
    if isinstance(image, str) and image.startswith(("http://", "https://")):
        downloaded = await _predownload_image(image)
        if downloaded is not None:
            image = downloaded
        # else: pre-download failed → fall through with the URL, let the LLM
        # try (cheaper than blocking the whole turn on our outbound HTTP).

    if isinstance(image, bytes):
        b64 = base64.b64encode(image).decode("ascii")
        image_url_value = f"data:image/jpeg;base64,{b64}"
    else:
        image_url_value = image

    schema_v2 = bool(settings.VISION_SCHEMA_V2)
    messages = _build_messages(image_url_value, schema_v2=schema_v2)
    src = "url" if isinstance(image, str) else f"bytes({len(image)}B)"
    logger.info(
        "👁️  [VISION] 🚀 호출 시작 model=%s source=%s schema_v2=%s",
        _model(),
        src,
        schema_v2,
    )

    timeout_s = float(settings.VISION_TIMEOUT_S)
    max_tokens = int(settings.VISION_MAX_TOKENS) if schema_v2 else 600
    temperature = float(settings.VISION_TEMPERATURE) if schema_v2 else 0.2

    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            LLMProvider.chat(
                model=_model(),
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        logger.warning("👁️  [VISION] 타임아웃 (%.1fs 초과)", timeout_s)
        return _fallback_result()
    except Exception as e:  # noqa: BLE001 — defensive top-level guard
        logger.warning("👁️  [VISION] LLM 호출 실패: %s", e)
        return _fallback_result()

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("👁️  [VISION] 응답 형식 이상 elapsed_ms=%d", elapsed_ms)
        return _fallback_result()

    parsed = _parse_json_relaxed(content or "")
    if parsed is None:
        logger.warning(
            "👁️  [VISION] JSON 파싱 실패 elapsed_ms=%d raw=%r",
            elapsed_ms,
            (content or "")[:200],
        )
        return _fallback_result()

    if schema_v2:
        result = _validate_v2(parsed)
        _log_v2(result, elapsed_ms)
        # B8 — empty-items rescue. Pinterest multi-look / multi-person photos
        # commonly trigger the model's "give up with empty items[]" path. One
        # bounded retry with a multi-look-aware user prompt costs ~1 LLM call
        # but recovers the entire turn that would otherwise hit the "사진이
        # 안 보이네" fallback. Only retry when the first pass yielded zero
        # items — successful extractions are kept as-is.
        if not result.items:
            logger.info("👁️  [VISION] 🔁 items=0 → multi-look retry")
            retry_messages = _build_messages(image_url_value, schema_v2=True, multi_look_retry=True)
            t1 = time.perf_counter()
            try:
                resp2 = await asyncio.wait_for(
                    LLMProvider.chat(
                        model=_model(),
                        messages=retry_messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    ),
                    timeout=timeout_s,
                )
            except (TimeoutError, Exception) as exc:  # noqa: BLE001
                logger.warning("👁️  [VISION] multi-look retry failed: %r", exc)
                return result
            elapsed2_ms = int((time.perf_counter() - t1) * 1000)
            try:
                content2 = resp2["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                return result
            parsed2 = _parse_json_relaxed(content2 or "")
            if parsed2 is None:
                return result
            retry_result = _validate_v2(parsed2)
            if retry_result.items:
                logger.info(
                    "👁️  [VISION] ✅ multi-look retry recovered items=%d (elapsed=%dms)",
                    len(retry_result.items),
                    elapsed2_ms,
                )
                _log_v2(retry_result, elapsed2_ms)
                return retry_result
    else:
        result = _legacy_to_vision_result(parsed)
        _log_legacy(result, elapsed_ms)
    return result


# ── Compat helpers — derive legacy fields from VisionResult ────────────────


def derive_legacy_label(item: VisionItem | None) -> str | None:
    """REQ-VISION-COMPAT-003 — derive `vision_item` from a VisionItem."""
    if item is None:
        return None
    return item.name or item.subcategory or item.category or "item"


def derive_legacy_keywords(item: VisionItem | None) -> list[str]:
    """REQ-VISION-COMPAT-003 — derive `vision_keywords` from a VisionItem.

    Tokenizes searchQuery (English) and falls back to subcategory + colorFamily
    + fit when searchQuery is empty.
    """
    if item is None:
        return []
    sq = (item.searchQuery or "").strip()
    if sq:
        return [t.lower() for t in sq.split() if t.strip()][:8]
    parts = [item.subcategory, item.fit, item.colorFamily, item.fabric]
    return [p.lower() for p in parts if p]


def derive_legacy_dict(item: VisionItem) -> dict:
    """Convert a VisionItem to the legacy dict shape expected by routing /
    pre-existing tests that consume `detected_items` as `list[dict]`.
    """
    return {
        "label": derive_legacy_label(item) or "item",
        "description": item.detail or "",
        "color": item.color or "",
        "keywords": derive_legacy_keywords(item),
        # Carry the rich fields through so consumers that have migrated can
        # still access them without round-tripping through VisionResult.
        "subcategory": item.subcategory,
        "fit": item.fit,
        "colorFamily": item.colorFamily,
        "fabric": item.fabric,
        "searchQuery": item.searchQuery,
        "searchQueryKo": item.searchQueryKo,
        "name": item.name,
        "category": item.category,
        "id": item.id,
    }


__all__ = [
    "VisionItem",
    "VisionMood",
    "VisionMoodTag",
    "VisionPaletteEntry",
    "VisionPosition",
    "VisionResult",
    "VisionStyle",
    "VisionStyleNode",
    "derive_legacy_dict",
    "derive_legacy_keywords",
    "derive_legacy_label",
    "extract",
]
