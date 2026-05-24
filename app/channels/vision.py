"""Vision extraction via LiteLLM proxy (GPT-4o-mini default).

SPEC-VISION-UNIFY-001 — emits the rich `VisionResult` schema mirroring
kikoai/app's `analyze.ts`. The v2 schema is the only path.

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


def _legacy_to_vision_result(legacy: dict) -> VisionResult:
    """Adapt a legacy {items:[{label,description,color,keywords}]} dict into a
    VisionResult so callers always see the same return type.

    The legacy minimal-schema PROMPT was removed (v2 is the only path now), but
    this coercion is still used by `app/graphs/nodes/vision.py` to normalize a
    monkeypatched/legacy dict-shaped `extract` result (REQ-VISION-COMPAT-001).
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
                # Legacy dict has no searchQuery/Ko — leave empty so downstream
                # keyword-based fallbacks still work.
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


def _build_messages(image_url_value: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": ANALYZE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": ANALYZE_USER_PROMPT},
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


# ── Public API ──────────────────────────────────────────────────────────────


@observe(name="vision_extract")
async def extract(image: str | bytes) -> VisionResult:
    """Run the Vision LLM and return a `VisionResult`.

    Never raises — any failure path returns the documented fallback
    (REQ-VISION-COMPAT-004).
    """
    if isinstance(image, bytes):
        b64 = base64.b64encode(image).decode("ascii")
        image_url_value = f"data:image/jpeg;base64,{b64}"
    else:
        image_url_value = image

    messages = _build_messages(image_url_value)
    src = "url" if isinstance(image, str) else f"bytes({len(image)}B)"
    logger.info(
        "👁️  [VISION] 🚀 호출 시작 model=%s source=%s",
        _model(),
        src,
    )

    timeout_s = float(settings.VISION_TIMEOUT_S)
    max_tokens = int(settings.VISION_MAX_TOKENS)
    temperature = float(settings.VISION_TEMPERATURE)

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

    result = _validate_v2(parsed)
    _log_v2(result, elapsed_ms)
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
