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
from agent_cdp import ConnectionType, EventScope, ResultContract

from spectragent.core.capabilities.browser.watchdogs.events import (
    ExtractContentEvent,
    ScreenshotRequestEvent,
)


class FakeScope(EventScope):
    """Real request dispatch with a direct fixture executor and emitted payloads."""

    def __init__(self) -> None:
        super().__init__("action-fixture", max_history_size=0)
        self.emitted: list[Any] = []
        self.connect_all(
            self.respond,
            mode=ConnectionType.DIRECT,
            receiver_key="browser.action",
            filter=lambda event: not isinstance(event, ScreenshotRequestEvent),
            result_contract=ResultContract(self.validate),
        )

    def respond(self, event: Any) -> Any:
        return "fixture text" if isinstance(event, ExtractContentEvent) else None

    @staticmethod
    def validate(value: Any) -> bool:
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError(value["error"])
        return True

    def emit(self, event: Any) -> Any:
        self.emitted.append(event)
        return super().emit(event)


class AnsweringScope(FakeScope):
    """Answers through a declared executor; business errors retain their cause."""

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        super().__init__()

    def respond(self, event: Any) -> Any:
        return self.answer


@dataclass
class FakeElementNode:
    """The two fields an action reads off a selector-map node to address it."""

    backend_node_id: int | None = None
    session_id: str | None = None


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
    _selector_maps: dict[str, dict[int, FakeElementNode]] = field(default_factory=dict)
    _cdp: Any = None
    _default_session_id: str | None = None

    @property
    def action_scope(self) -> FakeScope:
        return self._scopes[self.default_session_id]

    @property
    def selector_map(self) -> dict[int, FakeElementNode]:
        return self._selector_maps.get(self.default_session_id, {})

    @property
    def default_session_id(self) -> str:
        if self._default_session_id is None:
            raise RuntimeError("No active browser session")
        return self._default_session_id


def answering_capability(answer: dict[str, Any]) -> FakeCapability:
    """A capability whose focused tab's handler answers every event with *answer*."""
    capability = FakeCapability()
    capability._scopes["sid_main"] = AnsweringScope(answer)
    capability._selector_maps["sid_main"] = {}
    capability._default_session_id = "sid_main"
    return capability


@pytest.fixture
def fake_capability() -> FakeCapability:
    """Focus a single session named ``sid_main`` with an empty selector map."""
    cap = FakeCapability()
    cap._scopes["sid_main"] = FakeScope()
    cap._selector_maps["sid_main"] = {}
    cap._default_session_id = "sid_main"
    return cap
