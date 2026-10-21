"""Comparing two screenshots: the restorer's 64-bit dHash and a 32-px grid of changed regions.

The dHash and its Hamming distance are spectragent's own, public in the
browser restoration module (the restorer's screenshot distance). The grid
difference is the method ``spectragent flywheel diagnose`` states in its
public ``DifferenceMethod`` (a pixel changed when its largest RGB channel
difference exceeds ``pixel_threshold``; a grid cell is active when at least
``cell_fraction`` of its pixels changed; four-neighbour connected cells become
one box; no resizing). The two small functions that apply it are private to
the diagnostics module, so they are re-implemented here.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass, field

from PIL import Image, ImageChops, ImageDraw, UnidentifiedImageError

from spectragent.core.capabilities.browser.restoration import dhash, hamming
from spectragent.flywheel.diagnostics import DifferenceMethod

#: ``[x0, y0, x1, y1]`` in pixels of the image, right and bottom exclusive.
Box = list[int]


def decode(data: bytes) -> Image.Image | None:
    """The image as RGB, or ``None`` when the bytes do not decode."""
    try:
        with Image.open(io.BytesIO(data)) as opened:
            return opened.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def distance(left: bytes, right: bytes) -> int | None:
    """The Hamming distance of the two images' dHash, or ``None`` when one does not decode."""
    a, b = dhash(left), dhash(right)
    if a is None or b is None:
        return None
    return hamming(a, b)


@dataclass(frozen=True)
class ChangedRegion:
    """One four-neighbour connected group of active grid cells.

    ``box`` bounds it (clipped to the image); ``area`` is the area of its
    cells themselves (clipped), and ``fraction`` that area over the image's.
    """

    box: Box
    cells: int
    area: int
    fraction: float


@dataclass
class Difference:
    """Where two same-sized images differ; ``reason`` says why nothing was compared."""

    compared: bool
    regions: list[ChangedRegion] = field(default_factory=list)
    changed_pixels: int | None = None
    total_pixels: int | None = None
    reason: str | None = None

    @property
    def boxes(self) -> list[Box]:
        return [region.box for region in self.regions]

    @property
    def changed_fraction(self) -> float | None:
        """Changed pixels over all pixels; ``None`` when nothing was compared."""
        if self.changed_pixels is None or not self.total_pixels:
            return None
        return self.changed_pixels / self.total_pixels

    def significant(self, min_fraction: float) -> list[ChangedRegion]:
        """The regions whose cell area is at least *min_fraction* of the image."""
        return [region for region in self.regions if region.fraction >= min_fraction]


def changed_cells(
    left: Image.Image, right: Image.Image, method: DifferenceMethod
) -> tuple[int, set[tuple[int, int]]]:
    """The number of changed pixels and the active grid cells (column, row)."""
    difference = ImageChops.difference(left, right)
    red, green, blue = difference.split()
    largest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    changed = largest.point([255 if value > method.pixel_threshold else 0 for value in range(256)])
    cells: set[tuple[int, int]] = set()
    grid = method.grid_size
    for y in range(0, left.height, grid):
        for x in range(0, left.width, grid):
            tile = changed.crop((x, y, min(x + grid, left.width), min(y + grid, left.height)))
            if tile.histogram()[255] / (tile.width * tile.height) >= method.cell_fraction:
                cells.add((x // grid, y // grid))
    return changed.histogram()[255], cells


def cell_regions(
    cells: set[tuple[int, int]], size: tuple[int, int], grid: int
) -> list[ChangedRegion]:
    """Four-neighbour connected cells as regions, top to bottom, then left to right."""
    width, height = size
    pending = set(cells)
    regions: list[ChangedRegion] = []
    while pending:
        start = min(pending)
        pending.remove(start)
        group, stack = {start}, [start]
        while stack:
            x, y = stack.pop()
            for cell in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if cell in pending:
                    pending.remove(cell)
                    group.add(cell)
                    stack.append(cell)
        x0 = min(c[0] for c in group) * grid
        y0 = min(c[1] for c in group) * grid
        x1 = min((max(c[0] for c in group) + 1) * grid, width)
        y1 = min((max(c[1] for c in group) + 1) * grid, height)
        area = sum(
            (min((cx + 1) * grid, width) - cx * grid) * (min((cy + 1) * grid, height) - cy * grid)
            for cx, cy in group
        )
        regions.append(
            ChangedRegion(
                box=[x0, y0, x1, y1],
                cells=len(group),
                area=area,
                fraction=area / (width * height),
            )
        )
    return sorted(regions, key=lambda region: (region.box[1], region.box[0]))


def cell_boxes(cells: set[tuple[int, int]], size: tuple[int, int], grid: int) -> list[Box]:
    """The boxes of :func:`cell_regions`."""
    return [region.box for region in cell_regions(cells, size, grid)]


def difference(left: bytes, right: bytes, method: DifferenceMethod | None = None) -> Difference:
    """The changed regions between two images of the same size."""
    method = method or DifferenceMethod()
    a, b = decode(left), decode(right)
    if a is None or b is None:
        return Difference(compared=False, reason="an image does not decode")
    if a.size != b.size:
        return Difference(
            compared=False, reason=f"image sizes differ: {a.size} vs {b.size}; not resized"
        )
    count, cells = changed_cells(a, b, method)
    return Difference(
        compared=True,
        regions=cell_regions(cells, a.size, method.grid_size),
        changed_pixels=count,
        total_pixels=a.width * a.height,
    )


def clip(box: Sequence[float], size: tuple[int, int]) -> Box | None:
    """*box* inside an image of *size* (rounded to whole pixels); ``None`` when nothing is left."""
    width, height = size
    x0, y0 = max(0, round(box[0])), max(0, round(box[1]))
    x1, y1 = min(width, round(box[2])), min(height, round(box[3]))
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def overlaps(box: Box, other: Box) -> bool:
    return box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]


@dataclass(frozen=True)
class Marker:
    """A point to ring on an image, in image pixels, with a short label."""

    label: str
    x: float
    y: float
    color: str


def mark(data: bytes, markers: list[Marker]) -> bytes:
    """The image with each marker drawn as a labelled ring, as PNG; the input when it does not decode."""
    image = decode(data)
    if image is None or not markers:
        return data
    draw = ImageDraw.Draw(image)
    radius = max(10, round(min(image.size) / 60))
    for marker in markers:
        box = (marker.x - radius, marker.y - radius, marker.x + radius, marker.y + radius)
        draw.ellipse(box, outline=marker.color, width=3)
        draw.text((marker.x + radius + 2, marker.y - radius), marker.label, fill=marker.color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
