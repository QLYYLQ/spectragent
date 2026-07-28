"""Shared fixtures for per-action unit tests.

A ``FakeScope`` records emitted CDP events so we can assert the action
produced the right event type without a real CDP connection. The
``fake_capability`` fixture hands back a BrowserCapability-lookalike
that exposes just the attributes the action classes touch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest


class FakeScope:
    """A minimal EventScope replacement that captures emitted events."""

    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, event: Any) -> None:
        self.emitted.append(event)


class AwaitableEvent:
    """Wraps a CDP event so the action can ``await event`` without a scope.

    Actions call ``scope.emit(event); await event`` where the real
    ``agent_cdp`` event is a Future-like object that the watchdog
    fulfills. For tests we just make it a no-op awaitable.
    """

    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped

    def __await__(self):  # pragma: no cover — trivial
        if False:
            yield None
        return None


@dataclass
class FakeElementNode:
    backend_node_id: int | None = None


@dataclass
class FakeCapability:
    """Mirrors the attributes BrowserCapability exposes to actions.

    Actions access ``_scopes``, ``_selector_maps`` and ``_cdp``; we give
    them matching fields so tests can drive ``execute`` without spinning
    up a real browser.

    ``default_session_id`` is the focused tab. Actions read it off the
    capability instead of receiving a ``session_id`` argument — a CDP tab
    handle is not something the framework can produce, so it is not in
    the Action ABI.
    """

    _scopes: dict[str, FakeScope] = field(default_factory=dict)
    _selector_maps: dict[str, dict[int, FakeElementNode]] = field(
        default_factory=dict
    )
    _cdp: Any = None
    _default_session_id: str | None = None

    @property
    def default_session_id(self) -> str:
        if self._default_session_id is None:
            raise RuntimeError("No active browser session")
        return self._default_session_id


@pytest.fixture
def fake_capability() -> FakeCapability:
    """Focus a single session named ``sid_main`` with an empty selector map."""
    cap = FakeCapability()
    cap._scopes["sid_main"] = FakeScope()
    cap._selector_maps["sid_main"] = {}
    cap._default_session_id = "sid_main"
    return cap
