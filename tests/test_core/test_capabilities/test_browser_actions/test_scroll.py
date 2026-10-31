"""ScrollAction — verifies capability-side config (default_amount / max_amount)."""

from __future__ import annotations

from spectragent.core.capabilities.browser.actions.scroll import ScrollAction
from spectragent.core.capabilities.browser.watchdogs.events import ScrollEvent
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
)


class TestScrollConfig:
    """``default_amount`` / ``max_amount`` reflect in the emitted event."""

    async def test_default_amount_used_when_params_omit(
        self, fake_capability: FakeCapability
    ) -> None:
        action = ScrollAction(default_amount=600)
        await action.execute(
            {"direction": "down"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert isinstance(event, ScrollEvent)
        assert event.amount == 600
        assert event.direction == "down"

    async def test_max_amount_caps_requested_amount(self, fake_capability: FakeCapability) -> None:
        action = ScrollAction(default_amount=300, max_amount=500)
        await action.execute(
            {"direction": "up", "amount": 9999},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert event.amount == 500

    async def test_explicit_amount_under_cap_respected(
        self, fake_capability: FakeCapability
    ) -> None:
        action = ScrollAction(default_amount=300, max_amount=2000)
        await action.execute(
            {"direction": "down", "amount": 1200},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert event.amount == 1200

    async def test_default_direction_applied_when_missing(
        self, fake_capability: FakeCapability
    ) -> None:
        action = ScrollAction(default_direction="up")
        await action.execute(
            {},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert event.direction == "up"


class TestScrollStart:
    """A start point reaches the executor; without one the wheel turns where the pointer is."""

    async def test_the_start_point_rides_the_event(self, fake_capability: FakeCapability) -> None:
        await ScrollAction().execute(
            {"direction": "down", "start_x": 300, "start_y": 200},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert isinstance(event, ScrollEvent)
        assert (event.start_x, event.start_y, event.backend_node_id) == (300, 200, None)

    async def test_no_start_point_is_the_pointer(self, fake_capability: FakeCapability) -> None:
        await ScrollAction().execute(
            {"direction": "down"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert (event.start_x, event.start_y) == (None, None)
