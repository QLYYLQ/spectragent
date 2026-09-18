"""DragAction — the verb UI-TARS's prompt taught and nothing implemented.

``HumanMouse.drag`` existed and was reachable from nothing; the shipped
prompts advertised ``drag(start_point=..., end_point=...)`` and
``guarded_act`` rejected it every time.
"""

from __future__ import annotations

from spectragent.core.capabilities.browser.actions.drag import DragAction
from spectragent.core.capabilities.browser.watchdogs.events import DragEvent
from tests.test_core.test_capabilities.test_browser_actions.conftest import (
    FakeCapability,
)


class TestDrag:
    async def test_emits_event_with_both_endpoints(self, fake_capability: FakeCapability) -> None:
        result = await DragAction().execute(
            {"start_x": 10, "start_y": 20, "end_x": 30, "end_y": 40},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert result.success
        event = fake_capability._scopes["sid_main"].emitted[0]
        assert isinstance(event, DragEvent)
        assert (event.start_x, event.start_y) == (10, 20)
        assert (event.end_x, event.end_y) == (30, 40)

    async def test_a_missing_endpoint_is_named(self, fake_capability: FakeCapability) -> None:
        result = await DragAction().execute(
            {"start_x": 10, "start_y": 20, "end_x": 30},
            capability=fake_capability,  # type: ignore[arg-type]
        )
        assert not result.success
        assert "end_y" in (result.error or "")

    def test_descriptor_advertises_what_execute_reads(self) -> None:
        descriptor = DragAction().descriptor()
        assert descriptor.name == "browser.drag"
        assert set(descriptor.params) == {"start_x", "start_y", "end_x", "end_y"}
