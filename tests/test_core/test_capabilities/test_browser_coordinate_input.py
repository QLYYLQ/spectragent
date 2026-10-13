"""ActionWatchdog's coordinate-native input handlers.

``type_coordinate`` and ``drag`` are the two verbs UI-TARS's prompt has
always taught and nothing could execute. Both go through
:class:`HumanMouse` and the CDP calls that operate on the *current
focus*, so neither touches the selector_map — which a vision-only run
does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    ActionWatchdog,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    DragEvent,
    TypeAtCoordinateEvent,
)


@dataclass
class _Call:
    method: str
    params: dict[str, Any]


class _FakeCDP:
    def __init__(self) -> None:
        self.calls: list[_Call] = []

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(_Call(method=method, params=params or {}))
        return {}


def _watchdog(cdp: _FakeCDP) -> ActionWatchdog:
    # Empty selector map: exactly what a vision-only run has.
    return ActionWatchdog(cdp=cdp, selector_map_getter=dict)  # type: ignore[arg-type]


class TestTypeAtCoordinate:
    async def test_clicks_to_focus_then_clears_then_inserts(self) -> None:
        cdp = _FakeCDP()
        wd = _watchdog(cdp)

        result = await wd._on_type_at_coordinate(
            TypeAtCoordinateEvent(coordinate_x=10, coordinate_y=20, text="hi")
        )

        # The fake answers nothing for the focused-field read: unknown, not "not sensitive".
        assert result == {"typed": True, "length": 2, "sensitive": None}
        methods = [c.method for c in cdp.calls]
        # move + press + release, then select-all/backspace, the focus read, then insert.
        assert methods[:3] == ["Input.dispatchMouseEvent"] * 3
        assert methods[-1] == "Input.insertText"
        assert cdp.calls[-1].params == {"text": "hi"}

    async def test_never_calls_dom_focus(self) -> None:
        """No selector_map means no node id — the click is the focus."""
        cdp = _FakeCDP()
        await _watchdog(cdp)._on_type_at_coordinate(
            TypeAtCoordinateEvent(coordinate_x=1, coordinate_y=2, text="x")
        )
        assert "DOM.focus" not in {c.method for c in cdp.calls}

    async def test_clear_false_skips_the_select_all_sequence(self) -> None:
        cdp = _FakeCDP()
        await _watchdog(cdp)._on_type_at_coordinate(
            TypeAtCoordinateEvent(coordinate_x=1, coordinate_y=2, text="x", clear=False)
        )
        assert "Input.dispatchKeyEvent" not in {c.method for c in cdp.calls}

    async def test_the_click_lands_on_the_requested_pixel(self) -> None:
        cdp = _FakeCDP()
        await _watchdog(cdp)._on_type_at_coordinate(
            TypeAtCoordinateEvent(coordinate_x=137, coordinate_y=42, text="x")
        )
        pressed = next(
            c
            for c in cdp.calls
            if c.method == "Input.dispatchMouseEvent" and c.params.get("type") == "mousePressed"
        )
        assert (pressed.params["x"], pressed.params["y"]) == (137, 42)


class TestDrag:
    async def test_presses_at_the_start_and_releases_at_the_end(self) -> None:
        cdp = _FakeCDP()

        result = await _watchdog(cdp)._on_drag(
            DragEvent(start_x=10, start_y=20, end_x=30, end_y=40)
        )

        assert result == {"dragged": True, "from": [10, 20], "to": [30, 40]}
        mouse = [c for c in cdp.calls if c.method == "Input.dispatchMouseEvent"]
        pressed = next(c for c in mouse if c.params["type"] == "mousePressed")
        released = next(c for c in mouse if c.params["type"] == "mouseReleased")
        assert (pressed.params["x"], pressed.params["y"]) == (10, 20)
        assert (released.params["x"], released.params["y"]) == (30, 40)

    async def test_moves_between_press_and_release(self) -> None:
        """The gesture is a drag, not two clicks: the button stays down."""
        cdp = _FakeCDP()
        await _watchdog(cdp)._on_drag(DragEvent(start_x=0, start_y=0, end_x=99, end_y=99))
        types = [c.params["type"] for c in cdp.calls if c.method == "Input.dispatchMouseEvent"]
        press_at = types.index("mousePressed")
        release_at = types.index("mouseReleased")
        assert "mouseMoved" in types[press_at:release_at]


class TestHandlersAreConnected:
    def test_attach_registers_both_new_events(self) -> None:
        """A handler nothing routes to is a verb the run silently rejects."""

        class _Scope:
            def __init__(self) -> None:
                self.connected: list[type] = []

            def connect(self, event_type: type, handler: Any, **_kw: Any) -> None:
                self.connected.append(event_type)

        scope = _Scope()
        _watchdog(_FakeCDP()).attach(scope)  # type: ignore[arg-type]
        assert TypeAtCoordinateEvent in scope.connected
        assert DragEvent in scope.connected
