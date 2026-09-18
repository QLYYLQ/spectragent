"""Detect JS click event listeners via CDP Runtime.evaluate.

This is an OPTIONAL CDP call that BrowserUseCleaner can request.
It uses the DevTools-only ``getEventListeners()`` API (available via
``includeCommandLineAPI``).

Ported from browser-use ``DomService._get_all_trees()`` (service.py:429-516).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# JS expression that finds elements with click-related event listeners.
# getEventListeners() is a DevTools Console Utility — only available when
# includeCommandLineAPI is True.
_JS_DETECT_CLICK_LISTENERS = """
(() => {
    if (typeof getEventListeners !== 'function') return null;
    const els = [];
    const allElements = document.querySelectorAll('*');
    for (const el of allElements) {
        try {
            const listeners = getEventListeners(el);
            if (listeners.click || listeners.mousedown || listeners.mouseup
                || listeners.pointerdown || listeners.pointerup) {
                els.push(el);
            }
        } catch (e) {}
    }
    return els;
})()
"""


class CDPSendProtocol(Protocol):
    """Minimal CDP command interface — matches CDPCommandProtocol.send()."""

    async def __call__(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any] | None: ...


async def detect_js_click_listeners(
    cdp_send: CDPSendProtocol,
    session_id: str | None = None,
) -> set[int]:
    """Return ``backend_node_id`` set for elements with JS click/mouse listeners.

    Parameters
    ----------
    cdp_send:
        Callable matching ``cdp.send(method, params, session_id=...)``.
    session_id:
        CDP session ID (for multi-target).

    Returns
    -------
    set[int]
        Backend node IDs of elements with click-related JS event listeners.
        Empty set on failure.
    """
    try:
        # Step 1: Evaluate JS to find elements with click listeners.
        result = await cdp_send(
            "Runtime.evaluate",
            {
                "expression": _JS_DETECT_CLICK_LISTENERS,
                "includeCommandLineAPI": True,
                "returnByValue": False,
            },
            session_id=session_id,
        )
        if result is None:
            return set()

        result_object_id = result.get("result", {}).get("objectId")
        if not result_object_id:
            return set()

        # Step 2: Get array properties to access each element.
        array_props = await cdp_send(
            "Runtime.getProperties",
            {"objectId": result_object_id, "ownProperties": True},
            session_id=session_id,
        )
        if array_props is None:
            return set()

        # Step 3: Collect object IDs for array elements.
        element_object_ids: list[str] = []
        for prop in array_props.get("result", []):
            prop_name = prop.get("name", "") if isinstance(prop, dict) else ""
            if isinstance(prop_name, str) and prop_name.isdigit():
                prop_value = prop.get("value", {}) if isinstance(prop, dict) else {}
                if isinstance(prop_value, dict):
                    object_id = prop_value.get("objectId")
                    if object_id and isinstance(object_id, str):
                        element_object_ids.append(object_id)

        # Step 4: Resolve each object ID to a backend_node_id in parallel.
        async def _resolve(object_id: str) -> int | None:
            try:
                node_info = await cdp_send(
                    "DOM.describeNode",
                    {"objectId": object_id},
                    session_id=session_id,
                )
                if node_info:
                    return node_info.get("node", {}).get("backendNodeId")
            except Exception:
                pass
            return None

        backend_ids = await asyncio.gather(*[_resolve(oid) for oid in element_object_ids])
        js_ids: set[int] = {bid for bid in backend_ids if bid is not None}

        # Step 5: Cleanup — release the array object.
        with contextlib.suppress(Exception):
            await cdp_send(
                "Runtime.releaseObject",
                {"objectId": result_object_id},
                session_id=session_id,
            )

        logger.debug("Detected %d elements with JS click listeners", len(js_ids))
        return js_ids

    except Exception as exc:
        logger.debug("Failed to detect JS event listeners: %s", exc)
        return set()
