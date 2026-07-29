"""TypeCoordinateAction — the vision-only typing path.

``browser.type`` needs a selector_map index, and a vision-only run
negotiates no DOM fragment, so its selector_map is empty by construction.
UI-TARS declared ``type`` anyway and every typed step came back
``Missing param: 'index'``; this action is the executable half of that
verb.
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.actions.type_coordinate import (
    TypeCoordinateAction,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    TypeAtCoordinateEvent,
)
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
)


class TestTypeCoordinate:
    async def test_emits_event_with_coordinates_and_text(
        self, fake_capability: FakeCapability
    ) -> None:
        result = await TypeCoordinateAction().execute(
            {"coordinate_x": 100, "coordinate_y": 200, "text": "hello"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert isinstance(event, TypeAtCoordinateEvent)
        assert (event.coordinate_x, event.coordinate_y) == (100, 200)
        assert event.text == "hello"
        assert event.clear is True

    async def test_does_not_touch_the_selector_map(
        self, fake_capability: FakeCapability
    ) -> None:
        """The whole reason this class exists: no index, no DOM."""
        assert fake_capability._selector_maps["sid_main"] == {}
        result = await TypeCoordinateAction().execute(
            {"coordinate_x": 1, "coordinate_y": 2, "text": "x"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success

    async def test_missing_text_is_named_not_silently_empty(
        self, fake_capability: FakeCapability
    ) -> None:
        """``TypeAction`` defaulted a missing ``text`` to ``""`` and typed
        nothing, successfully. Here the omission is reported."""
        result = await TypeCoordinateAction().execute(
            {"coordinate_x": 1, "coordinate_y": 2},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert not result.success
        assert "text" in (result.error or "")

    async def test_clear_by_default_is_capability_side_config(
        self, fake_capability: FakeCapability
    ) -> None:
        await TypeCoordinateAction(clear_by_default=False).execute(
            {"coordinate_x": 1, "coordinate_y": 2, "text": "x"},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert fake_capability._scopes["sid_main"].emitted[0].clear is False

    async def test_an_explicit_clear_beats_the_default(
        self, fake_capability: FakeCapability
    ) -> None:
        await TypeCoordinateAction(clear_by_default=False).execute(
            {"coordinate_x": 1, "coordinate_y": 2, "text": "x", "clear": True},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert fake_capability._scopes["sid_main"].emitted[0].clear is True

    def test_descriptor_advertises_what_execute_reads(self) -> None:
        descriptor = TypeCoordinateAction().descriptor()
        assert descriptor.name == "browser.type_coordinate"
        assert set(descriptor.params) == {
            "coordinate_x",
            "coordinate_y",
            "text",
            "clear",
        }
