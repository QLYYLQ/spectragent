"""Shared helpers used by multiple browser action classes.

Keeps the per-action modules short while avoiding duplication for the
``index → backend_node_id`` translation that several actions need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


def resolve_index_to_backend_node_id(
    capability: BrowserCapability,
    session_id: str,
    index: Any,
) -> int:
    """Translate an LLM-facing ``index`` param into a CDP ``backend_node_id``.

    The per-tab selector_map is keyed by backend_node_id; the LLM output
    carries the same integer as ``index``. We simply look it up and
    return the node's backend_node_id.

    Raises:
        ValueError: If the element is missing from the selector_map.
    """
    selector_map = capability._selector_maps.get(session_id, {})
    node = selector_map.get(index)
    if node is None or node.backend_node_id is None:
        raise ValueError(f"Element {index} not found in selector_map")
    return node.backend_node_id
