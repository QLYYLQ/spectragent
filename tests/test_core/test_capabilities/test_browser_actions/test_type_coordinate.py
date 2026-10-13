"""TypeCoordinateAction — the vision-only typing path.

``browser.type`` needs a selector_map index, and a vision-only run
negotiates no DOM fragment, so its selector_map is empty by construction.
UI-TARS declared ``type`` anyway and every typed step came back
``Missing param: 'index'``; this action is the executable half of that
verb.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.type_coordinate import (
    TypeCoordinateAction,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    TypeAtCoordinateEvent,
)
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
    FakeScope,
)


class _AnsweringScope(FakeScope):
    """Answers every emitted event the way the action watchdog's handler would."""

    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__()
        self.answer = answer

    def emit(self, event: Any) -> None:
        super().emit(event)
        event.record_result(connection_id="c1", handler_name="ActionWatchdog", result=self.answer)


def _answering(answer: dict[str, Any]) -> FakeCapability:
    capability = FakeCapability()
    capability._scopes["sid_main"] = _AnsweringScope(answer)
    capability._selector_maps["sid_main"] = {}
    capability._default_session_id = "sid_main"
    return capability


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

    async def test_does_not_touch_the_selector_map(self, fake_capability: FakeCapability) -> None:
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

    @pytest.mark.parametrize(
        "error",
        [
            "mouse dispatch failed: target closed",  # the focusing click's own reply
            "Input.insertText failed",  # the text that follows it
        ],
        ids=["focusing click", "typing"],
    )
    async def test_a_handler_error_fails_the_action(self, error: str) -> None:
        """The handler answers ``{"error": ...}`` when the click that should focus the
        field, or the typing after it, did not happen; the action must not report
        success for text it never typed."""
        result = await TypeCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": 1, "coordinate_y": 2, "text": "x"},
            capability=_answering({"error": error}),  # type: ignore[arg-type]
        )
        assert (result.success, result.error) == (False, error)
        assert result.typed_into_sensitive_field is None

    async def test_a_performed_type_succeeds(self) -> None:
        result = await TypeCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": 1, "coordinate_y": 2, "text": "x"},
            capability=_answering({"typed": True, "length": 1, "sensitive": False}),  # type: ignore[arg-type]
        )
        assert (result.success, result.error) == (True, None)

    def test_descriptor_advertises_what_execute_reads(self) -> None:
        descriptor = TypeCoordinateAction().descriptor()
        assert descriptor.name == "browser.type_coordinate"
        assert set(descriptor.params) == {
            "coordinate_x",
            "coordinate_y",
            "text",
            "clear",
        }
