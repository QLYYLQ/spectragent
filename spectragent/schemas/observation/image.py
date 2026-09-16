"""What a screenshot fragment's bytes say about themselves.

A capture arrives as raw bytes and three readers need to know what encoding
they are: the resolver that fills
:class:`~spectragent.schemas.observation.geometry.ScreenshotGeometry`
(pixel size), the WAL (file extension), and a vision agent (the
``data:`` URL's media type, which Anthropic validates and rejects when it
disagrees with the payload).

All three read the **bytes**, never a declared field. A format carried
alongside the image is a second source of truth that can disagree with it —
a capability configured for JPEG whose provider answered with PNG, a
fragment rebuilt from an on-disk file — and the disagreement surfaces as a
provider-side 400 several layers away. The headers are 24 bytes and already
in hand, so deriving costs nothing and cannot drift.

Only the encodings a capture path in this framework can produce are
recognised (PNG, JPEG, WebP, GIF). Unrecognised bytes report size ``(0, 0)``
— which :func:`~spectragent.schemas.observation.geometry.point_grid` already
treats as "size unknown, refuse the point" — and media type ``None``, so a
caller decides for itself rather than being handed a plausible-looking lie.
"""

from __future__ import annotations

from typing import Literal

ImageMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
"""Media types a screenshot fragment can carry.

Deliberately the same members as
:data:`spectragent.core.llm.messages.SupportedImageMediaType`: the value
goes straight into an ``ImageURL``. They are separate declarations because
an observation schema must not depend on the LLM layer.
"""

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: JPEG frame markers carrying a height/width pair. Everything in the SOF
#: family except DHT (0xC4), DNL (0xC8) and DAC (0xCC), which share the
#: 0xC0-0xCF block without being frame headers.
_JPEG_SOF_MARKERS = frozenset(
    set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
)

#: Extension each media type is persisted under. ``.jpg`` rather than
#: ``.jpeg`` to match what ``browser.screenshot`` (the action) already
#: writes for its own artifacts.
_SUFFIXES: dict[ImageMediaType, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def image_media_type(data: bytes) -> ImageMediaType | None:
    """The media type *data* declares in its own header, or ``None``.

    ``None`` means "these bytes are not an image this framework knows how
    to label" — never a guess. Handing a caller ``image/png`` for bytes
    that are not a PNG is how a JPEG ends up inside a ``data:image/png``
    URL, which OpenAI sniffs past and Anthropic rejects.
    """
    if data.startswith(_PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def image_suffix(data: bytes) -> str | None:
    """The file extension (with dot) *data* should be persisted under.

    ``None`` for bytes :func:`image_media_type` does not recognise, so the
    caller can refuse rather than write an image under a name that lies
    about it — a trace reader globs by extension.
    """
    media_type = image_media_type(data)
    return None if media_type is None else _SUFFIXES[media_type]


def image_size(data: bytes) -> tuple[int, int]:
    """``(width, height)`` in pixels, read from *data*'s own header.

    ``(0, 0)`` when the bytes are truncated or of an encoding without a
    reader here — the value
    :func:`~spectragent.schemas.observation.geometry.point_grid` already
    reads as "unknown", which makes it refuse a point instead of placing
    it on an assumed grid.

    The size is not the viewport size — a capture is rendered at the
    device scale factor — and it is already in the bytes the resolver
    holds, so reading the header keeps an observe cycle at the one CDP
    round trip negotiation paid for.
    """
    if data.startswith(_PNG_SIGNATURE):
        return _png_size(data)
    if data.startswith(b"\xff\xd8\xff"):
        return _jpeg_size(data)
    return 0, 0


def _png_size(data: bytes) -> tuple[int, int]:
    """Width/height out of the IHDR chunk, which a PNG always opens with."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return 0, 0
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Width/height out of the first SOF segment.

    A JPEG's size is not at a fixed offset the way a PNG's is: the frame
    header sits behind a variable run of application, quantisation and
    comment segments, so the segment chain has to be walked. Each segment
    is ``0xFF <marker> <2-byte length including itself>``; the standalone
    markers (``RSTn``, ``SOI``, ``TEM``) carry no length and are skipped by
    marker value.
    """
    pos, end = 2, len(data)
    while pos + 3 < end:
        if data[pos] != 0xFF:
            # Fill bytes are legal between segments; anything else means
            # the stream is not one we can walk.
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xFF, 0x01) or 0xD0 <= marker <= 0xD9:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if length < 2:
            return 0, 0
        if marker in _JPEG_SOF_MARKERS:
            if pos + 9 > end:
                return 0, 0
            height = int.from_bytes(data[pos + 5 : pos + 7], "big")
            width = int.from_bytes(data[pos + 7 : pos + 9], "big")
            return width, height
        pos += 2 + length
    return 0, 0
