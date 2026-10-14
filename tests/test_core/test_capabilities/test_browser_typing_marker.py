"""Typing records whether the text went into a sensitive field (§5.6).

Right before the text goes in, the focused element is judged by the shared
sensitivity rule (``dom/sensitivity.py``); the verdict travels from the
watchdog's reply into ``ActionResult.typed_into_sensitive_field``. Unknown
is ``None``, never "not sensitive". The real-Chrome counterpart is
``test_browser_archive_live.py::test_privacy_guard_on_the_dom_path``; that no
agent renders the marker is ``tests/test_agents/test_action_result_marker_prompts.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.type_action import TypeAction
from spectragent.core.capabilities.browser.actions.type_coordinate import TypeCoordinateAction
from spectragent.core.capabilities.browser.dom.models import DOMRect
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import (
    ActionWatchdog,
    _focused_field_verdict,
)
from spectragent.core.capabilities.browser.watchdogs.events import (
    TypeAtCoordinateEvent,
    TypeTextEvent,
)
from tests.test_core.test_capabilities.test_browser_action_click_robustness import (
    FakeCDP,
    _make_node,
)
from tests.test_core.test_capabilities.test_browser_action_frames import (
    _AnsweringScope,
    _capability,
)


def _focused(tag: str, **attrs: str) -> dict[str, Any]:
    """A ``Runtime.evaluate`` reply describing the focused element."""
    return {"result": {"type": "object", "value": {"tag": tag, "attrs": attrs}}}


_NOTHING_FOCUSED = {"result": {"type": "object", "subtype": "null", "value": None}}


class TestTheVerdict:
    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            (_focused("INPUT", type="password", id="pw"), True),
            (_focused("INPUT", type="text", autocomplete="one-time-code"), True),
            (_focused("INPUT", type="tel", name="cc-number", autocomplete="cc-number"), True),
            (_focused("INPUT", type="text", id="otp-code"), True),
            (_focused("TEXTAREA", name="verification_code"), True),
            (_focused("INPUT", type="text", id="search"), False),
            (_focused("TEXTAREA", id="comment"), False),
            (_focused("DIV", id="password-editor"), False),  # not a form control
            (_focused("BODY"), False),
            (_NOTHING_FOCUSED, False),
        ],
    )
    def test_the_shared_rule_judges_the_focused_element(
        self, reply: dict[str, Any], expected: bool
    ) -> None:
        assert _focused_field_verdict(reply) is expected

    def test_attribute_names_are_read_case_insensitively(self) -> None:
        reply = {"result": {"value": {"tag": "input", "attrs": {"TYPE": "password"}}}}
        assert _focused_field_verdict(reply) is True

    @pytest.mark.parametrize(
        "reply",
        [
            {"result": {"type": "object", "value": {"opaque": True}}},  # another process
            {"exceptionDetails": {"text": "Uncaught"}, "result": {"type": "object"}},
            {"result": {"type": "undefined"}},
            {},
            None,
        ],
        ids=["opaque frame", "threw", "undefined", "empty", "none"],
    )
    def test_what_cannot_be_seen_is_unknown_not_safe(self, reply: Any) -> None:
        assert _focused_field_verdict(reply) is None


class _FocusCDP(FakeCDP):
    """Answers the focused-element read with a canned reply."""

    def __init__(self, focused: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.focused = focused

    async def send(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None
    ) -> dict[str, Any]:
        reply = await super().send(method, params, session_id=session_id)
        if method == "Runtime.evaluate":
            return self.focused
        return reply


def _watchdog(cdp: FakeCDP) -> ActionWatchdog:
    node = _make_node(42, DOMRect(x=400.0, y=300.0, width=100.0, height=40.0))
    return ActionWatchdog(input_platform="linux", cdp=cdp, selector_map_getter=lambda: {42: node})  # type: ignore[arg-type]


class TestTheHandlerReportsIt:
    async def test_typing_into_a_password_field(self) -> None:
        cdp = _FocusCDP(_focused("INPUT", type="password"))
        result = await _watchdog(cdp)._on_type_text(
            TypeTextEvent(backend_node_id=42, text="hunter2", clear=False)
        )
        assert result == {"typed": True, "length": 7, "sensitive": True}

    async def test_typing_into_a_plain_field(self) -> None:
        cdp = _FocusCDP(_focused("INPUT", type="search"))
        result = await _watchdog(cdp)._on_type_at_coordinate(
            TypeAtCoordinateEvent(coordinate_x=10, coordinate_y=20, text="shoes", clear=False)
        )
        assert result == {"typed": True, "length": 5, "sensitive": False}

    async def test_the_field_is_judged_before_the_text_goes_in(self) -> None:
        cdp = _FocusCDP(_focused("INPUT", type="password"))
        await _watchdog(cdp)._on_type_text(TypeTextEvent(backend_node_id=42, text="x", clear=True))
        methods = [c.method for c in cdp.calls]
        assert methods.index("Runtime.evaluate") < methods.index("Input.insertText")
        read = next(c for c in cdp.calls if c.method == "Runtime.evaluate")
        assert read.params["returnByValue"] is True

    async def test_a_failed_read_is_unknown_and_the_text_still_goes_in(self) -> None:
        cdp = FakeCDP(raise_on={"Runtime.evaluate"})
        result = await _watchdog(cdp)._on_type_text(
            TypeTextEvent(backend_node_id=42, text="hi", clear=False)
        )
        assert result == {"typed": True, "length": 2, "sensitive": None}
        assert "Input.insertText" in [c.method for c in cdp.calls]


def _fake(answer: dict[str, Any]) -> Any:
    """A capability lookalike whose focused tab answers every event with *answer*."""
    return _capability(_AnsweringScope(answer))


class TestTheActionRecordsIt:
    @pytest.mark.parametrize("verdict", [True, False, None])
    async def test_type_by_index(self, verdict: bool | None) -> None:
        result = await TypeAction(post_delay_ms=0).execute(
            {"index": 2**31 + 10, "text": "x"},
            capability=_fake({"typed": True, "length": 1, "sensitive": verdict}),
        )
        assert result.success, result.error
        assert result.typed_into_sensitive_field is verdict

    @pytest.mark.parametrize("verdict", [True, False, None])
    async def test_type_at_a_coordinate(self, verdict: bool | None) -> None:
        result = await TypeCoordinateAction(post_delay_ms=0).execute(
            {"coordinate_x": 1, "coordinate_y": 2, "text": "x"},
            capability=_fake({"typed": True, "length": 1, "sensitive": verdict}),
        )
        assert result.success, result.error
        assert result.typed_into_sensitive_field is verdict

    async def test_a_handler_that_says_nothing_leaves_it_unset(self) -> None:
        result = await TypeAction(post_delay_ms=0).execute(
            {"index": 2**31 + 10, "text": "x"}, capability=_fake({"typed": True, "length": 1})
        )
        assert result.typed_into_sensitive_field is None

    async def test_a_refused_type_carries_no_verdict(self) -> None:
        result = await TypeAction(post_delay_ms=0).execute(
            {"index": 2**31 + 10, "text": "x"},
            capability=_fake({"error": "The element is inside a frame that changed"}),
        )
        assert not result.success
        assert result.typed_into_sensitive_field is None
