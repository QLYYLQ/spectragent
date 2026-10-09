"""Screenshot → LLM content part.

Screenshots reach an evaluator as real image files under the trace's
``observations/`` directory — ``.png``, or ``.jpg`` when the run captured
JPEG observations — listed by ``RunTrace.screenshots`` from the manifest's
fragment index. So the default path is "read the bytes, base64 them, label
them by extension" — no re-encode, no quality loss, no PIL round-trip.

Downscaling is opt-in and exists for one reason: a stepwise judge that
feeds forty full-resolution screenshots into one request is expensive
enough that the original grader shrank them to 20%. When it is requested
the image is re-encoded as JPEG, and the media type follows — the grader
this replaces emitted JPEG bytes under a ``data:image/png`` prefix, which
works on OpenAI (it sniffs) and is rejected by Anthropic (it does not).
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Literal

from spectragent.core.llm.messages import (
    ContentPartImageParam,
    ImageURL,
    SupportedImageMediaType,
)

_SUFFIX_MEDIA_TYPE: dict[str, SupportedImageMediaType] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def image_part(
    path: Path,
    *,
    detail: str = "high",
    scale: float = 1.0,
) -> ContentPartImageParam:
    """Build one image content part from an on-disk screenshot."""
    if scale >= 1.0:
        data = path.read_bytes()
        media_type = _SUFFIX_MEDIA_TYPE.get(path.suffix.lower(), "image/png")
    else:
        data, media_type = _rescaled_jpeg(path, scale)

    encoded = base64.b64encode(data).decode("ascii")
    return ContentPartImageParam(
        image_url=ImageURL(
            url=f"data:{media_type};base64,{encoded}",
            detail=_detail_literal(detail),
            media_type=media_type,
        )
    )


def _rescaled_jpeg(path: Path, scale: float) -> tuple[bytes, SupportedImageMediaType]:
    from PIL import Image

    with Image.open(path) as img:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        width = max(1, int(img.width * scale))
        height = max(1, int(img.height * scale))
        resized = img.resize((width, height))
        buffer = io.BytesIO()
        resized.save(buffer, format="JPEG")
    return buffer.getvalue(), "image/jpeg"


def _detail_literal(detail: str) -> Literal["auto", "low", "high"]:
    if detail == "low":
        return "low"
    if detail == "high":
        return "high"
    return "auto"
