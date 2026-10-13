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
    answering_capability,
)


class TestClickCoordinate:
    async def test_emits_event_with_coordinates(self, fake_capability: FakeCapability) -> None:
        await ClickCoordinateAction().execute(
            {"coordinate_x": 100, "coordinate_y": 200},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert isinstance(event, ClickCoordinateEvent)
        assert event.coordinate_x == 100
        assert event.coordinate_y == 200

    async def test_missing_coordinates_fails(self, fake_capability: FakeCapability) -> None:
        result = await ClickCoordinateAction().execute(
            {"coordinate_x": 10},  # missing coordinate_y
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is False

    async def test_does_not_touch_selector_map(self, fake_capability: FakeCapability) -> None:
        # No selector_map entries at all.
        result = await ClickCoordinateAction().execute(
            {"coordinate_x": 1, "coordinate_y": 2},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success is True

    async def test_a_handler_error_fails_the_action(self) -> None:
        """The handler answers ``{"error": ...}`` when the mouse input was not sent
        (the tab went away, the dispatch was refused); the action must not report
        success for a click that never happened."""
        error = "Input.dispatchMouseEvent failed: target closed"
        result = await ClickCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": 1, "coordinate_y": 2},
            capability=answering_capability({"error": error}),  # type: ignore[arg-type]
        )
        assert (result.success, result.error) == (False, error)

    async def test_a_performed_click_succeeds(self) -> None:
        result = await ClickCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": 1, "coordinate_y": 2},
            capability=answering_capability({"clicked": True, "x": 1, "y": 2}),  # type: ignore[arg-type]
        )
        assert (result.success, result.error) == (True, None)
