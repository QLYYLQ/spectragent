"""The screenshot difference hash every restoring capability measures with (``core/image_hash.py``)."""

from __future__ import annotations

import io

from PIL import Image

from spectragent.core.image_hash import dhash, hamming


def picture(seed: int = 0, size: tuple[int, int] = (64, 36), fmt: str = "PNG") -> bytes:
    image = Image.new("RGB", size)
    pixels = image.load()
    assert pixels is not None
    for y in range(size[1]):
        for x in range(size[0]):
            pixels[x, y] = ((x * 7 + seed * 97) % 256, (y * 11 + seed * 31) % 256, (x * y) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def test_identical_bytes_are_zero_apart() -> None:
    a = dhash(picture(1))
    assert a is not None and hamming(a, dhash(picture(1)) or 0) == 0


def test_the_same_pixels_in_another_encoding_are_close() -> None:
    a, b = dhash(picture(1)), dhash(picture(1, fmt="JPEG"))
    assert a is not None and b is not None and hamming(a, b) <= 5


def test_another_picture_is_far() -> None:
    a, b = dhash(picture(1)), dhash(picture(2))
    assert a is not None and b is not None and hamming(a, b) > 5


def test_bytes_that_are_no_image() -> None:
    assert dhash(b"\x89PNG\r\n\x1a\nnot really") is None
