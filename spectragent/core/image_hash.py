"""The 64-bit difference hash of a screenshot, and the distance between two of them.

A restored observation's screenshot is compared with the recorded one by the
Hamming distance of their difference hashes (``docs/architecture_design.md``
§4.14 "恢复：重定位与校验", §11.2 "分支回放"). Each capability that checks
its restorations measures its own screenshots this way, masking first what it
knows to be volatile (the browser its media areas, Android its status bar),
and the flywheel's diagnostics compare recorded screenshots the same way, so
the hash lives outside any one capability.

Pillow, grayscale, resized to 9x8 with Lanczos (so the hash does not depend on
a library default), one bit per horizontally adjacent pixel pair, set when the
left one is brighter.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)


def grayscale(image_bytes: bytes) -> Image.Image | None:
    """The image in grayscale, or ``None`` (with a warning) when the bytes do not decode."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            return image.convert("L")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning("screenshot does not decode for the perceptual hash: %s", exc)
        return None


def image_dhash(image: Image.Image) -> int:
    """The difference hash of an image already decoded, in any mode (converted to grayscale first).

    For a reader that changes the image before hashing it, such as one that
    masks parts of it.
    """
    gray = image if image.mode == "L" else image.convert("L")
    small = gray.resize((9, 8), Image.Resampling.LANCZOS)
    pixels = small.tobytes()  # mode "L": one byte per pixel, row by row
    bits = 0
    for row in range(8):
        for col in range(8):
            left, right = pixels[row * 9 + col], pixels[row * 9 + col + 1]
            bits = (bits << 1) | int(left > right)
    return bits


def dhash(image_bytes: bytes) -> int | None:
    """The 64-bit difference hash of an image, or ``None`` when the bytes do not decode."""
    gray = grayscale(image_bytes)
    return image_dhash(gray) if gray is not None else None


def hamming(a: int, b: int) -> int:
    """How many bits differ between two hashes."""
    return (a ^ b).bit_count()


__all__ = ["dhash", "grayscale", "hamming", "image_dhash"]
