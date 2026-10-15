"""Cursor evidence and a pure raster overlay for browser observations."""

from __future__ import annotations

from io import BytesIO
from math import ceil

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field


class ScreenshotCursor(BaseModel):
    """The observed pointer hotspot in CSS viewport coordinates.

    ``None`` means the position is unknown, rather than the viewport origin.
    The rendering settings travel with the screenshot as reproducible evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    position: tuple[int, int] | None = None
    size_css_px: int = Field(default=32, ge=16, le=96)
    image_quality: int = Field(default=80, ge=0, le=100)


def render_cursor(
    image: bytes,
    cursor: ScreenshotCursor,
    *,
    viewport_width: int,
    viewport_height: int,
) -> bytes:
    """Draw the pointer into a new PNG/JPEG without changing capture geometry.

    The actual image dimensions determine each axis's CSS-to-image scale. An
    unknown position or viewport leaves even the original byte object intact.
    Decode/format errors propagate to the caller; they never manufacture evidence.
    """
    if cursor.position is None or viewport_width <= 0 or viewport_height <= 0:
        return image

    with Image.open(BytesIO(image)) as source:
        image_format = source.format
        if image_format not in {"PNG", "JPEG"}:
            raise ValueError(f"Cursor overlay requires PNG or JPEG, got {image_format!r}")
        canvas = source.convert("RGBA")

    scale_x = canvas.width / viewport_width
    scale_y = canvas.height / viewport_height
    size = cursor.size_css_px
    # A local tile bounds both the halo and arrow, so supersampling does not
    # multiply the memory cost of the full screenshot.
    width = max(1, ceil(2 * size * scale_x))
    height = max(1, ceil(2 * size * scale_y))
    supersample = 4
    tile = Image.new("RGBA", (width * supersample, height * supersample))
    draw = ImageDraw.Draw(tile)
    unit_x = size * scale_x * supersample
    unit_y = size * scale_y * supersample

    def point(x: float, y: float) -> tuple[float, float]:
        return ((x + 0.5) * unit_x, (y + 0.5) * unit_y)

    draw.ellipse([point(-0.40, -0.40), point(0.40, 0.40)], fill=(255, 218, 45, 85))
    arrow = [
        point(0, 0),
        point(0, 0.88),
        point(0.21, 0.67),
        point(0.38, 1.00),
        point(0.55, 0.91),
        point(0.38, 0.59),
        point(0.67, 0.59),
    ]
    draw.polygon(arrow, fill=(255, 255, 255, 255))
    draw.line(
        [*arrow, arrow[0]],
        fill=(20, 20, 20, 255),
        width=max(1, round(min(unit_x, unit_y) * 0.055)),
        joint="curve",
    )
    tile = tile.resize((width, height), Image.Resampling.LANCZOS)
    x, y = cursor.position
    canvas.alpha_composite(
        tile,
        (round((x - size * 0.5) * scale_x), round((y - size * 0.5) * scale_y)),
    )
    output = BytesIO()
    if image_format == "JPEG":
        canvas.convert("RGB").save(output, format="JPEG", quality=cursor.image_quality)
    else:
        canvas.save(output, format="PNG")
    return output.getvalue()
