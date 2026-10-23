"""The fragment format descriptions a run hands an agent's ``prepare``, for tests calling it directly.

``AgentRuntime.prepare()`` reads each quoted fragment's description from the
environment (``Environment.fragment_formats``; the browser's comes from its
DOM serializer) and hands the agent the ones its ``__fragment_formats__``
declares (``core/negotiation/fragment_formats.py``). A test that calls an
agent's ``prepare`` itself passes what a run with that serializer would: the
serializer's own words, never a hand-written stand-in.
"""

from __future__ import annotations

from typing import Any

from spectragent.core.capabilities.browser.dom.serializers import (
    DOMSerializerProtocol,
    default_serializer,
)


def dom_formats(serializer: DOMSerializerProtocol | None = None) -> dict[str, str]:
    """``{"dom": <description>}`` for *serializer* (the browser's default when omitted)."""
    return {"dom": (serializer or default_serializer()).describe_format()}


def formats_for(agent: Any, serializer: DOMSerializerProtocol | None = None) -> dict[str, Any]:
    """The ``fragment_formats`` keyword a browser run hands *agent*'s ``prepare``.

    ``{}`` for an agent that declares no quoted formats (UI-TARS): the runtime
    passes the keyword only to an agent that declares it, so a test spreading
    this into a call matches the run for every agent class.
    """
    cls = agent if isinstance(agent, type) else type(agent)
    declared = getattr(cls, "__fragment_formats__", None)
    if declared is None:
        return {}
    assert declared <= {"dom"}, f"{cls.__name__} quotes {sorted(declared)}"
    return {"fragment_formats": dom_formats(serializer)}
