"""What typing or a key press measurably did: ``TextInputEffect``.

``docs/architecture_design.md`` §4.5, "动作效果": the text-input actions report
the control that had keyboard focus, whether its value changed (compared in
the page, never recorded), the named keys and chords they sent, whether text
was entered, and the focused element afterwards. A sensitive field's content
and length appear nowhere. Measured by the executor around its own dispatch;
a probe that fails or runs out of ``action_effect_timeout_s`` leaves its fact
unknown and never changes the action's outcome; a failed action reports no
effect. One failure does: typing finds no text field with focus (nothing, or
an element that takes no typed text), sends no key and no text, and reports
the focus it found.

Every test runs the real action against a real ``ActionWatchdog`` on a
scripted CDP (``_effect_tab.py``). The real-Chrome counterpart is
``test_browser_effects_pointer_live.py``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from spectragent.core.capabilities.browser.actions.send_keys import SendKeysAction
from spectragent.core.capabilities.browser.actions.type_action import TypeAction
from spectragent.core.capabilities.browser.actions.type_coordinate import TypeCoordinateAction
from spectragent.core.capabilities.browser.dom.models import DOMElementNode
from spectragent.core.capabilities.browser.effects import ElementFacts, TextInputEffect
from spectragent.core.capabilities.browser.platform import InputPlatform
from spectragent.core.capabilities.browser.watchdogs.action_watchdog import FOCUS_SETTLE_S
from spectragent.schemas.action import ActionResult
from tests.test_core.test_capabilities.test_browser_actions._effect_tab import (
    ProbeCDP,
    effect_tab,
    facts,
    page_node,
    reading,
)

NAME = ElementFacts(role="textbox", name="Name")
PASSWORD = ElementFacts(role="textbox", name="Password")
SECRET = "correct-horse-9"  # 15 characters: neither it nor its length may appear


def name_field(*digests: str | None) -> list[dict[str, Any]]:
    return [reading("textbox", "Name", digest=d, id="name") for d in digests]


def password_field(*digests: str | None) -> list[dict[str, Any]]:
    return [reading("textbox", "Password", digest=d, type="password", id="pw") for d in digests]


async def type_into(
    cdp: ProbeCDP,
    text: str = "Ada",
    *,
    clear: bool | None = None,
    platform: InputPlatform = "linux",
    node: DOMElementNode | None = None,
    **watchdog_kwargs: Any,
) -> ActionResult:
    """``browser.type`` into index 3."""
    params: dict[str, Any] = {"index": 3, "text": text}
    if clear is not None:
        params["clear"] = clear
    nodes = {3: node if node is not None else page_node(42)}
    async with effect_tab(cdp, nodes=nodes, platform=platform, **watchdog_kwargs) as tab:
        return await TypeAction(post_delay_ms=0).execute(params, capability=tab)  # type: ignore[arg-type]


async def type_at(
    cdp: ProbeCDP, text: str = "Ada", *, clear: bool | None = None, **watchdog_kwargs: Any
) -> ActionResult:
    """``browser.type_coordinate`` at (100, 150)."""
    params: dict[str, Any] = {"coordinate_x": 100, "coordinate_y": 150, "text": text}
    if clear is not None:
        params["clear"] = clear
    async with effect_tab(cdp, **watchdog_kwargs) as tab:
        return await TypeCoordinateAction(post_delay_ms=0).execute(params, capability=tab)  # type: ignore[arg-type]


async def press(cdp: ProbeCDP, keys: str, **watchdog_kwargs: Any) -> ActionResult:
    async with effect_tab(cdp, **watchdog_kwargs) as tab:
        return await SendKeysAction(post_delay_ms=0).execute({"keys": keys}, capability=tab)  # type: ignore[arg-type]


def text_input(result: ActionResult) -> TextInputEffect:
    assert result.success, result.error
    assert isinstance(result.effect, TextInputEffect)
    return result.effect


class TestDeclared:
    @pytest.mark.parametrize("action", [TypeAction, TypeCoordinateAction, SendKeysAction])
    def test_the_text_input_actions_share_the_text_input_effect(self, action: type) -> None:
        assert action.Effect is TextInputEffect


class TestType:
    async def test_typing_with_the_default_clear(self) -> None:
        cdp = ProbeCDP(
            field="node",
            readings=name_field("0:1", "3:2"),
            focus={"facts": facts("textbox", "Name")},
        )
        result = await type_into(cdp)
        effect = text_input(result)
        assert effect.effect_id == "browser.type"
        assert (effect.target, effect.sensitive, effect.value_changed) == (NAME, False, True)
        assert effect.keys == ("Control+A", "Backspace")
        assert (effect.text_entered, effect.focus_after) == (True, NAME)
        assert result.typed_into_sensitive_field is False
        assert effect.describe() == (
            'Pressed Control+A, Backspace, then typed text into textbox "Name"; '
            'its value changed; focus is now on textbox "Name".'
        )

    async def test_the_clear_is_written_as_the_platforms_select_all(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1", "3:2"))
        effect = text_input(await type_into(cdp, platform="macos"))
        assert effect.keys == ("Meta+A", "Backspace")

    async def test_the_same_text_again_leaves_the_value_unchanged(self) -> None:
        cdp = ProbeCDP(
            field="node",
            readings=name_field("3:2", "3:2"),
            focus={"facts": facts("textbox", "Name")},
        )
        effect = text_input(await type_into(cdp))
        assert effect.value_changed is False
        assert effect.describe() == (
            'Pressed Control+A, Backspace, then typed text into textbox "Name"; '
            'its value did not change; focus is now on textbox "Name".'
        )

    async def test_without_a_clear_no_key_is_listed(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1", "3:2"))
        effect = text_input(await type_into(cdp, clear=False))
        assert effect.keys == ()
        assert effect.describe() == (
            'Typed text into textbox "Name"; its value changed; nothing is focused now.'
        )

    async def test_an_empty_text_enters_no_text(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("3:2", "0:1"))
        effect = text_input(await type_into(cdp, ""))
        assert (effect.keys, effect.text_entered, effect.value_changed) == (
            ("Control+A", "Backspace"),
            False,
            True,
        )

    async def test_the_field_is_read_before_the_clear_and_after_the_text(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1", "3:2"))
        await type_into(cdp)
        first_key = cdp.index("Input.dispatchKeyEvent")
        insert = cdp.index("Input.insertText")
        assert cdp.index("field_ref") < cdp.index("field_read") < first_key
        assert insert < cdp.index("field_read", last=True) < cdp.index("focus")
        assert cdp.calls[cdp.index("release")].params == {"objectId": "field"}

    async def test_a_sensitive_field_shows_neither_content_nor_length(self) -> None:
        cdp = ProbeCDP(
            field="node",
            readings=password_field("0:1", f"{len(SECRET)}:77"),
            focus={"facts": facts("textbox", "Password")},
        )
        result = await type_into(cdp, SECRET)
        effect = text_input(result)
        assert (effect.target, effect.sensitive, effect.value_changed) == (PASSWORD, True, True)
        assert result.typed_into_sensitive_field is True
        line = effect.describe()
        assert line == (
            'Pressed Control+A, Backspace, then typed text into textbox "Password" '
            "(a sensitive field; its content is not shown); its value changed; "
            'focus is now on textbox "Password".'
        )
        for shown in (effect.model_dump_json(), line):
            assert SECRET not in shown
            assert str(len(SECRET)) not in shown

    async def test_an_element_in_a_frame_is_read_in_its_own_session_and_focus_on_the_tab(
        self,
    ) -> None:
        node = page_node(11)
        node.session_id = "frame"
        cdp = ProbeCDP(field="node", readings=name_field("0:1", "3:2"), focus={"opaque": True})
        effect = text_input(await type_into(cdp, node=node))
        assert cdp.calls[cdp.index("field_ref")].session_id == "frame"
        assert cdp.calls[cdp.index("focus")].session_id == "tab"
        assert (effect.target, effect.focus_after) == (NAME, "unavailable")


class TestTypeCoordinate:
    async def test_the_click_measures_nothing_and_the_typing_is_measured(self) -> None:
        cdp = ProbeCDP(
            field="node",
            readings=name_field("0:1", "3:2"),
            focus={"facts": facts("textbox", "Name")},
        )
        effect = text_input(await type_at(cdp))
        assert effect.effect_id == "browser.type_coordinate"
        assert (effect.target, effect.value_changed, effect.text_entered) == (NAME, True, True)
        assert "hit" not in cdp.kinds()
        assert cdp.index("mouseReleased") < cdp.index("field_ref")


#: What typing at (100, 150) answers when the click left nothing focused.
NOTHING_FOCUSED = "No text field took focus at (100, 150): nothing has focus, so no text was typed."


def refused(result: ActionResult, error: str) -> TextInputEffect:
    """A typing action that found no text field: failed, with the focus it found as its effect."""
    assert (result.success, result.error) == (False, error)
    # No text went in, so there is no field whose sensitivity the text met.
    assert result.typed_into_sensitive_field is None
    assert isinstance(result.effect, TextInputEffect)
    assert (result.effect.keys, result.effect.text_entered) == ((), False)
    return result.effect


def keys_and_text(cdp: ProbeCDP) -> list[str]:
    """Every key event and text insertion sent: what reached the page as typing."""
    return [c.method for c in cdp.inputs() if c.method != "Input.dispatchMouseEvent"]


class TestTypingNeedsATextField:
    """No text field has focus: no clear, no text, and the action fails saying what has focus.

    A select-all with nothing editable focused selects the whole page's text,
    and the text then goes nowhere.
    """

    async def test_a_click_that_focused_nothing_types_nothing(self) -> None:
        cdp = ProbeCDP(field="null")
        effect = refused(await type_at(cdp), NOTHING_FOCUSED)
        assert (effect.target, effect.sensitive, effect.value_changed, effect.focus_after) == (
            "nothing",
            False,
            "unavailable",
            "nothing",
        )
        assert "mouseReleased" in [c.params.get("type") for c in cdp.inputs()]
        assert keys_and_text(cdp) == []

    async def test_without_a_clear_nothing_is_typed_either(self) -> None:
        cdp = ProbeCDP(field="null")
        refused(await type_at(cdp, clear=False), NOTHING_FOCUSED)
        assert keys_and_text(cdp) == []

    async def test_an_element_that_is_not_a_text_field_is_named(self) -> None:
        node = page_node(42)
        node.element_index = 3
        cdp = ProbeCDP(
            field="node",
            readings=[reading("button", "Search", digest="6:1", tag="BUTTON", entry="none")],
            focus={"facts": facts("button", "Search")},
        )
        effect = refused(
            await type_into(cdp, node=node),
            'Element 3 is not a text field: focus is on button "Search", which does not '
            "take typed text, so no text was typed.",
        )
        search = ElementFacts(role="button", name="Search")
        assert (effect.target, effect.value_changed, effect.focus_after) == (search, False, search)
        assert cdp.inputs() == []
        # The element was focused, and the field reference is let go.
        assert "DOM.focus" in cdp.kinds()
        assert "release" in cdp.kinds()

    async def test_a_read_only_field_is_said_to_be_one(self) -> None:
        cdp = ProbeCDP(
            field="node", readings=[reading("textbox", "From", digest="8:1", entry="read-only")]
        )
        effect = refused(
            await type_at(cdp),
            'No text field took focus at (100, 150): focus is on textbox "From", which is '
            "read-only, so no text was typed.",
        )
        assert effect.target == ElementFacts(role="textbox", name="From")
        assert keys_and_text(cdp) == []

    async def test_focus_that_lands_a_moment_later_is_waited_for(self) -> None:
        # A widget that focuses its input on a timer after the click.
        cdp = ProbeCDP(field=["null", "null", "node"], readings=name_field("0:1", "3:2"))
        effect = text_input(await type_at(cdp))
        assert (effect.target, effect.keys, effect.value_changed) == (
            NAME,
            ("Control+A", "Backspace"),
            True,
        )
        assert cdp.kinds().count("field_ref") == 3

    async def test_the_wait_for_focus_is_bounded(self) -> None:
        cdp = ProbeCDP(field="null")
        started = time.monotonic()
        refused(await type_at(cdp), NOTHING_FOCUSED)
        elapsed = time.monotonic() - started
        assert FOCUS_SETTLE_S <= elapsed < FOCUS_SETTLE_S + 0.5
        assert cdp.kinds().count("field_ref") > 1

    async def test_a_reading_that_cannot_tell_does_not_undo_one_that_could(self) -> None:
        # Nothing focused, then a focused element whose reading fails: the
        # last measured verdict stands, and the effect says what the last
        # reading could not tell.
        cdp = ProbeCDP(field=["null", "node"], readings=name_field("0:1"), fail={"field_read"})
        effect = refused(await type_at(cdp), NOTHING_FOCUSED)
        assert effect.target == "unknown"
        assert keys_and_text(cdp) == []

    @pytest.mark.parametrize(
        ("field", "readings"),
        [
            # Focus inside a frame of another site: it cannot be looked into.
            ("opaque", []),
            # A custom element whose shadow root is closed: it may hold the field.
            ("node", [reading("generic", "", digest=None, tag="X-FIELD", entry=None)]),
        ],
        ids=["frame of another site", "closed shadow root"],
    )
    async def test_a_reading_that_cannot_tell_types_as_asked(
        self, field: str, readings: list[dict[str, Any]]
    ) -> None:
        cdp = ProbeCDP(field=field, readings=readings)
        text_input(await type_at(cdp))
        assert keys_and_text(cdp) == ["Input.dispatchKeyEvent"] * 6 + ["Input.insertText"]


class TestSendKeys:
    async def test_a_named_key_into_a_field(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("3:2", "3:2"))
        effect = text_input(await press(cdp, "enter"))
        assert effect.effect_id == "browser.send_keys"
        assert (effect.keys, effect.text_entered, effect.value_changed) == (
            ("Enter",),
            False,
            False,
        )
        assert effect.describe() == (
            'Pressed Enter into textbox "Name"; its value did not change; nothing is focused now.'
        )

    async def test_a_chord_with_nothing_focused(self) -> None:
        # Written as the parser writes it (``KeyChord.text``): the modifier
        # canonical and, held with Control, the letter in capitals, as the
        # clear's select-all is written.
        effect = text_input(await press(ProbeCDP(field="null"), "ctrl+a"))
        assert (effect.target, effect.keys, effect.value_changed) == (
            "nothing",
            ("Control+A",),
            "unavailable",
        )
        assert (
            effect.describe() == "Pressed Control+A with nothing focused; nothing is focused now."
        )

    async def test_the_field_is_read_around_the_key(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1", "1:2"))
        await press(cdp, "a")
        assert cdp.index("field_read") < cdp.index("Input.dispatchKeyEvent")
        assert cdp.index("Input.dispatchKeyEvent", last=True) < cdp.index("field_read", last=True)

    async def test_a_character_into_a_plain_field_is_named(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1", "1:2"))
        effect = text_input(await press(cdp, "a"))
        assert (effect.keys, effect.text_entered) == (("a",), False)

    @pytest.mark.parametrize("keys", ["7", "Shift+Q", "Space"])
    async def test_a_character_into_a_sensitive_field_is_not_named(self, keys: str) -> None:
        cdp = ProbeCDP(field="node", readings=password_field("3:1", "4:2"))
        effect = text_input(await press(cdp, keys))
        assert (effect.keys, effect.text_entered, effect.sensitive) == ((), True, True)
        assert effect.describe() == (
            'Typed text into textbox "Password" (a sensitive field; its content is not shown); '
            "its value changed; nothing is focused now."
        )

    async def test_a_character_where_sensitivity_is_unknown_is_not_named_either(self) -> None:
        effect = text_input(await press(ProbeCDP(field="opaque"), "7"))
        assert (effect.target, effect.sensitive, effect.keys, effect.text_entered) == (
            "unavailable",
            "unavailable",
            (),
            True,
        )

    async def test_a_named_key_into_a_sensitive_field_is_named(self) -> None:
        cdp = ProbeCDP(field="node", readings=password_field("3:1", "3:1"))
        effect = text_input(await press(cdp, "Enter"))
        assert effect.keys == ("Enter",)

    async def test_text_is_refused_before_anything_is_measured(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1"))
        result = await press(cdp, "https://example.com/")
        assert not result.success
        assert (result.error or "").endswith("To enter text, use type.")
        assert result.effect is None
        assert cdp.calls == []

    async def test_a_key_that_was_not_sent_reports_no_effect(self) -> None:
        result = await press(ProbeCDP(fail_methods={"Input.dispatchKeyEvent"}), "Enter")
        assert not result.success
        assert result.effect is None


class TestMeasuringNeverDecidesTheOutcome:
    async def test_a_field_probe_that_fails_is_unknown(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1"), fail={"field_ref"})
        result = await type_into(cdp)
        effect = text_input(result)
        assert (effect.target, effect.sensitive, effect.value_changed) == (
            "unknown",
            "unknown",
            "unknown",
        )
        assert effect.describe() == (
            "Pressed Control+A, Backspace, then typed text into an unknown element; "
            "whether its value changed is unknown; nothing is focused now."
        )

    async def test_probes_that_never_answer_spend_the_budget_and_no_more(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1"), hang={"field_ref"})
        started = time.monotonic()
        result = await type_into(cdp, clear=False, action_effect_timeout_s=0.05)
        assert time.monotonic() - started < 1.0
        effect = text_input(result)
        assert (effect.target, effect.value_changed, effect.focus_after) == (
            "unknown",
            "unknown",
            "unknown",
        )
        # The sensitivity verdict was the probe's, and the probe said nothing.
        assert result.typed_into_sensitive_field is None
        assert "focus" not in cdp.kinds()
        assert [c.method for c in cdp.inputs()] == ["Input.insertText"]

    async def test_a_send_keys_probe_that_fails_leaves_the_key_pressed(self) -> None:
        cdp = ProbeCDP(fail={"field_ref", "focus"})
        effect = text_input(await press(cdp, "Escape"))
        assert (effect.target, effect.focus_after, effect.keys) == (
            "unknown",
            "unknown",
            ("Escape",),
        )
        assert [c.params["type"] for c in cdp.inputs()] == ["keyDown", "keyUp"]

    async def test_typing_that_failed_reports_no_effect(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1"), fail_methods={"Input.insertText"})
        result = await type_into(cdp)
        assert (result.success, result.effect, result.typed_into_sensitive_field) == (
            False,
            None,
            None,
        )
        # The field reference is let go all the same.
        assert "release" in cdp.kinds()

    async def test_a_coordinate_type_whose_click_failed_reports_no_effect(self) -> None:
        result = await type_at(ProbeCDP(fail_methods={"Input.dispatchMouseEvent"}))
        assert (result.success, result.effect) == (False, None)
