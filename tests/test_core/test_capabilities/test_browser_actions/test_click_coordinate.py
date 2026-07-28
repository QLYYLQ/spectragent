"""ClickCoordinateAction — vision-only click path (no selector map lookup)."""

from __future__ import annotations

from spectragent.core.capabilities.browser.actions.click_coordinate import (
    ClickCoordinateAction,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    ClickCoordinateEvent,
)
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
)


class TestClickCoordinate:
    async def test_emits_event_with_coordinates(
        self, fake_capability: FakeCapability
    ) -> None:
        await ClickCoordinateAction().execute(
            {"coordinate_x": 100, "coordinate_y": 200},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert isinstance(event, ClickCoordinateEvent)
        assert event.coordinate_x == 100
        assert event.coordinate_y == 200

    async def test_missing_coordinates_fails(
        self, fake_capability: FakeCapability
    ) -> None:
        result = await ClickCoordinateAction().execute(
            {"coordinate_x": 10},  # missing coordinate_y
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False

    async def test_does_not_touch_selector_map(
        self, fake_capability: FakeCapability
    ) -> None:
        # No selector_map entries at all.
        result = await ClickCoordinateAction().execute(
            {"coordinate_x": 1, "coordinate_y": 2},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is True
