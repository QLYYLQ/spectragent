"""DOM filter components — shared between cleaning strategies."""

from spectragent.core.capabilities.browser.dom.filters.bounds import propagate_parent_bounds
from spectragent.core.capabilities.browser.dom.filters.interactivity import (
    INTERACTIVE_ARIA_ROLES,
    INTERACTIVE_TAGS,
    is_interactive,
)
from spectragent.core.capabilities.browser.dom.filters.occlusion import filter_by_paint_order
from spectragent.core.capabilities.browser.dom.filters.visibility import is_visible

__all__ = [
    "INTERACTIVE_ARIA_ROLES",
    "INTERACTIVE_TAGS",
    "is_interactive",
    "is_visible",
    "filter_by_paint_order",
    "propagate_parent_bounds",
]
