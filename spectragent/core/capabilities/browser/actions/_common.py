"""Shared helpers used by multiple browser action classes.

Keeps the per-action modules short while avoiding duplication for the
``index → element`` translation that several actions need.

Both helpers resolve against the capability's **focused tab**
(``BrowserCapability.default_session_id``, moved by ``switch_tab``).
Actions take no session argument: the CDP session id is a browser
concept, and the framework Protocols an action sits behind must not
carry one. Concentrating the ``_scopes`` / ``_selector_maps`` reach here
also keeps that private surface out of the twelve action modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from spectragent.core.capabilities.browser.capability import BrowserCapability


class ElementRef(NamedTuple):
    """How an event addresses an element: CDP's own ``(session, backend id)``.

    ``session_id`` is the session of the target that owns the node — not the
    tab's for an element inside an out-of-process iframe, whose backend node
    id may well equal one in the page.
    """

    backend_node_id: int
    session_id: str | None


def focused_scope(capability: BrowserCapability) -> Any:
    """Return the ``EventScope`` of the capability's focused tab.

    Raises:
        RuntimeError: If no tab is attached (``setup()`` has not run).
        KeyError: If the focused tab has no scope — a torn-down session.
    """
    return capability._scopes[capability.default_session_id]


def resolve_index(
    capability: BrowserCapability,
    index: Any,
) -> ElementRef:
    """Translate an LLM-facing ``index`` param into the element it names.

    The focused tab's selector_map is keyed by the same integer the model
    reads as ``[index]`` (``dom.models.element_handle``); the element found
    there says where CDP can reach it.

    Raises:
        ValueError: If the element is missing from the selector_map.
    """
    selector_map = capability._selector_maps.get(
        capability.default_session_id, {}
    )
    node = selector_map.get(index)
    if node is None or node.backend_node_id is None:
        raise ValueError(f"Element {index} not found in selector_map")
    return ElementRef(node.backend_node_id, node.session_id)


def handler_error(event: Any) -> str | None:
    """The ``error`` a watchdog handler answered ``event`` with, if any.

    Element handlers (click, type) reply with a dict, and a dict carrying
    ``error`` means the action did not happen — the element was gone, or
    could not be reached where the last observation saw it. Reading it is
    what keeps an action from reporting success for a click it never sent.
    """
    for outcome in event.event_results.values():
        value = getattr(outcome, "result", None)
        if isinstance(value, dict) and value.get("error"):
            return str(value["error"])
    return None
