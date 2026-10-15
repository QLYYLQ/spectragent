"""The cursor overlay preserves screenshot geometry and uses CSS coordinates."""

from io import BytesIO

import pytest
from PIL import Image, ImageChops
from PIL.JpegImagePlugin import JpegImageFile
from pydantic import ValidationError

from spectragent.core.capabilities.browser.cursor import ScreenshotCursor, render_cursor


def _image(size: tuple[int, int], image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, (80, 100, 120)).save(output, format=image_format)
    return output.getvalue()


def _decoded(image: bytes) -> Image.Image:
    with Image.open(BytesIO(image)) as source:
        return source.convert("RGB")


def test_unknown_position_and_viewport_leave_original_bytes_untouched():
    # No decoding or accidental reencoding when the location cannot be known.
    image = b"not decoded"
    assert render_cursor(image, ScreenshotCursor(), viewport_width=100, viewport_height=80) is image
    for width, height in [(0, 80), (100, 0), (-1, 80)]:
        assert (
            render_cursor(
                image,
                ScreenshotCursor(position=(20, 30)),
                viewport_width=width,
                viewport_height=height,
            )
            is image
        )


@pytest.mark.parametrize("size", [(240, 160), (480, 320), (480, 160)])
def test_overlay_maps_each_axis_from_css_to_actual_image(size):
    image = _image(size)
    result = render_cursor(
        image, ScreenshotCursor(position=(80, 50)), viewport_width=240, viewport_height=160
    )
    decoded = _decoded(result)
    assert decoded.size == size
    changed = ImageChops.difference(decoded, _decoded(image)).getbbox()
    assert changed is not None
    left, top, right, bottom = changed
    scale_x, scale_y = size[0] / 240, size[1] / 160
    # Halo surrounds the hotspot; the arrow extends down and to the right.
    assert 64 * scale_x <= left < 80 * scale_x
    assert 34 * scale_y <= top < 50 * scale_y
    assert 100 * scale_x < right <= 107 * scale_x
    assert 80 * scale_y < bottom <= 85 * scale_y
    assert decoded.getpixel((round(82 * scale_x), round(58 * scale_y))) != (80, 100, 120)
    assert result == render_cursor(
        image, ScreenshotCursor(position=(80, 50)), viewport_width=240, viewport_height=160
    )


@pytest.mark.parametrize("position", [(0, 0), (239, 0), (0, 159), (239, 159), (-3, 50)])
def test_overlay_clips_at_viewport_edges(position):
    image = _image((240, 160))
    result = render_cursor(
        image, ScreenshotCursor(position=position), viewport_width=240, viewport_height=160
    )
    assert _decoded(result).size == (240, 160)
    assert ImageChops.difference(_decoded(result), _decoded(image)).getbbox() is not None


@pytest.mark.parametrize("image_format", ["PNG", "JPEG"])
def test_overlay_retains_actual_encoding(image_format):
    image = _image((240, 160), image_format)
    result = render_cursor(
        image, ScreenshotCursor(position=(80, 50)), viewport_width=240, viewport_height=160
    )
    with Image.open(BytesIO(result)) as decoded:
        assert decoded.format == image_format
        assert decoded.size == (240, 160)


def test_jpeg_reencoding_uses_quality_setting():
    image = _image((240, 160), "JPEG")
    low = render_cursor(
        image,
        ScreenshotCursor(position=(80, 50), image_quality=0),
        viewport_width=240,
        viewport_height=160,
    )
    high = render_cursor(
        image,
        ScreenshotCursor(position=(80, 50), image_quality=100),
        viewport_width=240,
        viewport_height=160,
    )
    with Image.open(BytesIO(low)) as low_image, Image.open(BytesIO(high)) as high_image:
        assert isinstance(low_image, JpegImageFile)
        assert isinstance(high_image, JpegImageFile)
        assert low_image.quantization != high_image.quantization
    assert len(low) < len(high)


def test_cursor_metadata_is_frozen_validated_and_roundtrips():
    cursor = ScreenshotCursor(position=(10, 20))
    assert ScreenshotCursor.model_validate_json(cursor.model_dump_json()) == cursor
    with pytest.raises(ValidationError, match="frozen"):
        cursor.position = (0, 0)
    for values in [
        {"size_css_px": 15},
        {"size_css_px": 97},
        {"image_quality": -1},
        {"image_quality": 101},
        {"unexpected": True},
    ]:
        with pytest.raises(ValidationError):
            ScreenshotCursor.model_validate(values)
