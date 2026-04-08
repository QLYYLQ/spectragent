"""Paint-order occlusion filter — rectangle union algorithm.

Ported from browser-use ``PaintOrderRemover.calculate_paint_order``.

Algorithm:
1. Collect all nodes that have both ``paint_order`` and ``bounds``.
2. Group by paint_order (descending — topmost first).
3. For each group: check ALL nodes first, then add opaque rects to union.
   (Elements in the same paint-order group never occlude each other.)
4. For each element: if fully contained by the union, mark as occluded.
5. Transparent / semi-transparent elements are excluded from the union.

Uses a proper disjoint rect-union with splitting (matching original
``RectUnionPure``) so multi-rect coverage is correctly detected.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from spectragent.core.capabilities.browser.dom.models import DOMElementNode


@dataclass(frozen=True, slots=True)
class _Rect:
    """Axis-aligned rectangle (x1,y1)-(x2,y2)."""

    x1: float
    y1: float
    x2: float
    y2: float

    def area(self) -> float:
        w = self.x2 - self.x1
        h = self.y2 - self.y1
        if w <= 0 or h <= 0:
            return 0.0
        return w * h

    def intersects(self, other: _Rect) -> bool:
        return not (
            self.x2 <= other.x1
            or other.x2 <= self.x1
            or self.y2 <= other.y1
            or other.y2 <= self.y1
        )

    def contains(self, other: _Rect) -> bool:
        return (
            self.x1 <= other.x1
            and self.y1 <= other.y1
            and self.x2 >= other.x2
            and self.y2 >= other.y2
        )


class _RectUnion:
    """Disjoint rectangle union with proper splitting.

    Matches original browser-use ``RectUnionPure``:
    - ``contains(r)`` checks if ``r`` is fully covered by the union of
      all added rectangles (not just a single one).
    - ``add(r)`` splits ``r`` into disjoint pieces before storing.
    """

    __slots__ = ("_rects",)

    def __init__(self) -> None:
        self._rects: list[_Rect] = []

    @staticmethod
    def _split_diff(a: _Rect, b: _Rect) -> list[_Rect]:
        """Return list of up to 4 rectangles = a \\ b."""
        parts: list[_Rect] = []
        # Bottom slice
        if a.y1 < b.y1:
            parts.append(_Rect(a.x1, a.y1, a.x2, b.y1))
        # Top slice
        if b.y2 < a.y2:
            parts.append(_Rect(a.x1, b.y2, a.x2, a.y2))
        # Middle vertical strip
        y_lo = max(a.y1, b.y1)
        y_hi = min(a.y2, b.y2)
        # Left slice
        if a.x1 < b.x1:
            parts.append(_Rect(a.x1, y_lo, b.x1, y_hi))
        # Right slice
        if b.x2 < a.x2:
            parts.append(_Rect(b.x2, y_lo, a.x2, y_hi))
        return parts

    def contains(self, r: _Rect) -> bool:
        """Return True if r is fully covered by the current union."""
        if not self._rects:
            return False
        stack = [r]
        for s in self._rects:
            new_stack: list[_Rect] = []
            for piece in stack:
                if s.contains(piece):
                    continue  # piece completely consumed
                if piece.intersects(s):
                    new_stack.extend(self._split_diff(piece, s))
                else:
                    new_stack.append(piece)
            if not new_stack:
                return True  # everything consumed
            stack = new_stack
        return False  # something survived

    def add(self, r: _Rect) -> None:
        """Insert r into the union (splits into disjoint pieces)."""
        if self.contains(r):
            return
        pending = [r]
        for s in self._rects:
            new_pending: list[_Rect] = []
            for piece in pending:
                if piece.intersects(s):
                    new_pending.extend(self._split_diff(piece, s))
                else:
                    new_pending.append(piece)
            pending = new_pending
        self._rects.extend(pending)


# Attributes that indicate transparency (element should not occlude).
_TRANSPARENT_BG = "rgba(0, 0, 0, 0)"
_MIN_OPAQUE_OPACITY = 0.8


def filter_by_paint_order(
    nodes: list[DOMElementNode],
    *,
    max_nodes: int = 5000,
) -> set[int]:
    """Return set of ``backend_node_id`` values that are occluded.

    Matches original browser-use ``PaintOrderRemover.calculate_paint_order``:
    - Groups elements by paint_order (descending — topmost first).
    - Within a group, ALL elements are checked BEFORE any are added to the
      union — so elements with the same paint_order never occlude each other.
    - Uses proper disjoint rect-union for multi-rect coverage detection.
    """
    # Collect candidates: nodes with paint_order + bounds.
    candidates: list[tuple[int, DOMElementNode]] = []
    for n in nodes:
        if n.paint_order is not None and n.bounds is not None and n.backend_node_id is not None:
            candidates.append((n.paint_order, n))

    if len(candidates) > max_nodes:
        return set()

    # Group by paint_order (descending — topmost first).
    grouped: defaultdict[int, list[DOMElementNode]] = defaultdict(list)
    for paint_order, node in candidates:
        grouped[paint_order].append(node)

    covered = _RectUnion()
    occluded: set[int] = set()

    for paint_order in sorted(grouped.keys(), reverse=True):
        group_nodes = grouped[paint_order]
        rects_to_add: list[_Rect] = []

        for node in group_nodes:
            b = node.bounds
            assert b is not None
            r = _Rect(b.x, b.y, b.x + b.width, b.y + b.height)

            # Check if fully covered by higher layers.
            # Note: zero-area elements (e.g. <source>) can still be covered
            # by the union — matching original browser-use behavior.
            if covered.contains(r):
                assert node.backend_node_id is not None
                occluded.add(node.backend_node_id)

            # Only opaque elements with non-zero area contribute to the
            # covered region.
            if r.area() > 0 and _is_opaque(node):
                rects_to_add.append(r)

        # Add all opaque rects from this group to the union.
        for r in rects_to_add:
            covered.add(r)

    return occluded


def _is_opaque(node: DOMElementNode) -> bool:
    """Heuristic: element is opaque enough to occlude elements below it.

    Matches original browser-use PaintOrderRemover logic:
    - Skip if background-color is transparent
    - Skip if opacity < 0.8
    """
    if not node.computed_styles:
        return False

    # Check background-color transparency.
    bg = node.computed_styles.get("background-color", _TRANSPARENT_BG)
    if bg == _TRANSPARENT_BG:
        return False

    # Check opacity.
    try:
        if float(node.computed_styles.get("opacity", "1")) < _MIN_OPAQUE_OPACITY:
            return False
    except (ValueError, TypeError):
        pass

    return True
