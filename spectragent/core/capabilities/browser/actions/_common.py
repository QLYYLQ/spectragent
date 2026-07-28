"""Shared helpers used by multiple browser action classes.

Keeps the per-action modules short while avoiding duplication for the
``index → backend_node_id`` translation that several actions need.

Both helpers resolve against the capability's **focused tab**
(``BrowserCapability.default_session_id``, moved by ``switch_tab``).
Actions take no session argument: the CDP session id is a browser
concept, and the framework Protocols an action sits behind must not
carry one. Concentrating the ``_scopes`` / ``_selector_maps`` reach here
also keeps that private surface out of the twelve action modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


def focused_scope(capability: BrowserCapability) -> Any:
    """Return the ``EventScope`` of the capability's focused tab.

    Raises:
        RuntimeError: If no tab is attached (``setup()`` has not run).
        KeyError: If the focused tab has no scope — a torn-down session.
    """
    return capability._scopes[capability.default_session_id]


def resolve_index_to_backend_node_id(
    capability: BrowserCapability,
    index: Any,
) -> int:
    """Translate an LLM-facing ``index`` param into a CDP ``backend_node_id``.

    The focused tab's selector_map is keyed by backend_node_id; the LLM
    output carries the same integer as ``index``. We simply look it up
    and return the node's backend_node_id.

    Raises:
        ValueError: If the element is missing from the selector_map.
    """
    selector_map = capability._selector_maps.get(
        capability.default_session_id, {}
    )
    node = selector_map.get(index)
    if node is None or node.backend_node_id is None:
        raise ValueError(f"Element {index} not found in selector_map")
    return node.backend_node_id
