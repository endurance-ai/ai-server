"""Vision extraction via LiteLLM proxy (GPT-4o-mini default).

Returns {"items": [{label, description, keywords, color}]}; never raises.
Each item describes one garment / accessory visible in the photo, capped at 4.
"""

import asyncio
import base64
import json
import logging
import re
import time

from app.core.config import settings
from app.observability.langfuse import observe
from app.providers.llm import LLMProvider

logger = logging.getLogger(__name__)

_VISION_TIMEOUT = 15.0
_MAX_ITEMS = 4

_SYSTEM_PROMPT = (
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

_FALLBACK_ITEM = {"label": "item", "description": "", "color": "", "keywords": []}
_FALLBACK = {"items": [dict(_FALLBACK_ITEM)]}


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


def _normalize_item(d: dict) -> dict:
    label = str(d.get("label") or d.get("item") or "").strip() or _FALLBACK_ITEM["label"]
    description = str(d.get("description") or "").strip()
    color = str(d.get("color") or "").strip()
    raw_kw = d.get("keywords") or []
    if not isinstance(raw_kw, list):
        raw_kw = []
    keywords = [str(k).strip().lower() for k in raw_kw if str(k).strip()]
    keywords = keywords[:8]
    return {"label": label, "description": description, "color": color, "keywords": keywords}


def _normalize(d: dict) -> dict:
    raw_items = d.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return dict(_FALLBACK)
    items = [_normalize_item(i) for i in raw_items if isinstance(i, dict)]
    items = items[:_MAX_ITEMS]
    if not items:
        return dict(_FALLBACK)
    return {"items": items}


@observe(name="vision_extract")
async def extract(image: str | bytes) -> dict:
    """Run vision model on either a URL string or raw bytes. Returns {"items": [...]} dict.
    On any failure returns the fallback shape with one placeholder item; never raises.
    """
    if isinstance(image, bytes):
        b64 = base64.b64encode(image).decode("ascii")
        image_url_value = f"data:image/jpeg;base64,{b64}"
    else:
        image_url_value = image

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "List every distinct fashion item in this photo as JSON only."},
                {"type": "image_url", "image_url": {"url": image_url_value}},
            ],
        },
    ]

    src = "url" if isinstance(image, str) else f"bytes({len(image)}B)"
    logger.info("👁️  [VISION] 🚀 호출 시작 model=%s source=%s", _model(), src)

    t0 = time.perf_counter()
    try:
        resp = await asyncio.wait_for(
            LLMProvider.chat(
                model=_model(),
                messages=messages,
                temperature=0.2,
                max_tokens=600,
            ),
            timeout=_VISION_TIMEOUT,
        )
    except TimeoutError:
        logger.warning("👁️  [VISION] 타임아웃 (%.1fs 초과)", _VISION_TIMEOUT)
        return dict(_FALLBACK)
    except Exception as e:
        logger.warning("👁️  [VISION] LLM 호출 실패: %s", e)
        return dict(_FALLBACK)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("👁️  [VISION] 응답 형식 이상 elapsed_ms=%d", elapsed_ms)
        return dict(_FALLBACK)

    parsed = _parse_json_relaxed(content or "")
    if parsed is None:
        logger.warning("👁️  [VISION] JSON 파싱 실패 elapsed_ms=%d raw=%r", elapsed_ms, (content or "")[:200])
        return dict(_FALLBACK)

    out = _normalize(parsed)
    logger.info("👁️  [VISION] ✅ 완료 elapsed=%dms items=%d", elapsed_ms, len(out["items"]))
    for i, it in enumerate(out["items"]):
        kw_preview = ", ".join(it.get("keywords", [])[:5])
        logger.info(
            "👁️  [VISION]   %s %s — %s [kw: %s]",
            ["1️⃣", "2️⃣", "3️⃣", "4️⃣"][i] if i < 4 else f"{i + 1}.",
            it.get("label", "?"),
            it.get("description", "")[:60],
            kw_preview,
        )
    return out
