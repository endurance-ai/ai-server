"""Vision extraction via LiteLLM proxy (GPT-4o-mini default).

Returns {"item","color","style","keywords"} dict; never raises.
"""

import asyncio
import base64
import json
import logging
import os
import re

from app.observability.langfuse import observe
from app.providers.llm import LLMProvider

logger = logging.getLogger(__name__)

_VISION_TIMEOUT = 15.0

_SYSTEM_PROMPT = (
    "You are a fashion vision tagger. Look at the provided image and respond with ONLY a "
    "single JSON object on one line, no prose, no code fence. Schema: "
    '{"item": string, "color": string, "style": string, "keywords": [string]}. '
    "`item` is a short English noun phrase (e.g. 'bomber jacket'). "
    "`color` is the dominant color in English. "
    "`style` is one short English style descriptor (e.g. 'streetwear', 'minimal'). "
    "`keywords` is an array of 3 to 10 lowercase English search keywords describing "
    "the garment, color, fit, fabric, and style. Respond with English only."
)

_FALLBACK = {"item": "item", "color": "", "style": "", "keywords": []}


def _model() -> str:
    return os.getenv("VISION_MODEL", "gpt-4o-mini")


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


def _normalize(d: dict) -> dict:
    item = str(d.get("item") or "").strip() or _FALLBACK["item"]
    color = str(d.get("color") or "").strip()
    style = str(d.get("style") or "").strip()
    raw_kw = d.get("keywords") or []
    if not isinstance(raw_kw, list):
        raw_kw = []
    keywords = [str(k).strip().lower() for k in raw_kw if str(k).strip()]
    keywords = keywords[:10]
    return {"item": item, "color": color, "style": style, "keywords": keywords}


@observe(name="vision_extract")
async def extract(image: str | bytes) -> dict:
    """Run vision model on either a URL string or raw bytes. Returns the four-field dict.
    On any failure returns the fallback shape with empty keywords; never raises.
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
                {"type": "text", "text": "Tag this fashion item as JSON only."},
                {"type": "image_url", "image_url": {"url": image_url_value}},
            ],
        },
    ]

    try:
        resp = await asyncio.wait_for(
            LLMProvider.chat(
                model=_model(),
                messages=messages,
                temperature=0.2,
                max_tokens=400,
            ),
            timeout=_VISION_TIMEOUT,
        )
    except TimeoutError:
        logger.warning("vision_extract timeout after %.1fs", _VISION_TIMEOUT)
        return dict(_FALLBACK)
    except Exception as e:
        logger.warning("vision_extract LLM call failed: %s", e)
        return dict(_FALLBACK)

    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("vision_extract unexpected response shape")
        return dict(_FALLBACK)

    parsed = _parse_json_relaxed(content or "")
    if parsed is None:
        logger.warning("vision_extract malformed JSON: %r", (content or "")[:200])
        return dict(_FALLBACK)

    return _normalize(parsed)
