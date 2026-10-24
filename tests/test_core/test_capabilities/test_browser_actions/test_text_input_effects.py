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
from spectragent.core.capabilities.browser.effects import (
    ElementFacts,
    KeyPressEffect,
    TextInputEffect,
)
from spectragent.core.capabilities.browser.platform import InputPlatform
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
    @pytest.mark.parametrize("action", [TypeAction, TypeCoordinateAction])
    def test_the_typing_actions_share_the_text_input_effect(self, action: type) -> None:
        assert action.Effect is TextInputEffect

    def test_send_keys_reports_a_text_input_effect_that_also_names_browser_shortcuts(
        self,
    ) -> None:
        """``KeyPressEffect`` (test_browser_shortcuts.py) is a ``TextInputEffect``: readers of one read both."""
        assert SendKeysAction.Effect is KeyPressEffect
        assert issubclass(KeyPressEffect, TextInputEffect)


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
NOTHING_FOCUSED = (
    "No text field took focus at the clicked point: nothing has focus, so no text was typed."
)


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
            readings=[reading("button", "Search", digest="6:1", tag="BUTTON", entry="no-role")],
            focus={"facts": facts("button", "Search")},
        )
        effect = refused(
            await type_into(cdp, node=node),
            'Typing into element 3 found no text field with focus: focus is on button "Search", '
            "which does not take typed text, so no text was typed.",
        )
        search = ElementFacts(role="button", name="Search")
        assert (effect.target, effect.value_changed, effect.focus_after) == (search, False, search)
        assert cdp.inputs() == []
        # The element was focused, the DOM agent saw no closed shadow root in
        # it, and the field reference is let go.
        assert "DOM.focus" in cdp.kinds()
        assert "describe" in cdp.kinds()
        assert "release" in cdp.kinds()

    async def test_an_element_with_a_role_that_is_no_text_role_is_refused_as_it_is(self) -> None:
        # A light-DOM component such as a select menu: its role says what it is.
        cdp = ProbeCDP(
            field="node",
            readings=[reading("combobox", "City", digest=None, tag="MAT-SELECT", entry="none")],
        )
        refused(
            await type_at(cdp),
            'No text field took focus at the clicked point: focus is on combobox "City", which '
            "does not take typed text, so no text was typed.",
        )
        assert "describe" not in cdp.kinds()
        assert keys_and_text(cdp) == []

    async def test_a_read_only_field_is_said_to_be_one(self) -> None:
        cdp = ProbeCDP(
            field="node", readings=[reading("textbox", "From", digest="8:1", entry="read-only")]
        )
        effect = refused(
            await type_at(cdp),
            'No text field took focus at the clicked point: focus is on textbox "From", which is '
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

    @pytest.mark.parametrize("wait_s", [0.0, 0.2])
    async def test_the_wait_for_focus_is_the_configured_one(self, wait_s: float) -> None:
        cdp = ProbeCDP(field="null")
        started = time.monotonic()
        refused(await type_at(cdp, type_focus_wait_s=wait_s), NOTHING_FOCUSED)
        elapsed = time.monotonic() - started
        assert wait_s <= elapsed < wait_s + 0.3
        reads = cdp.kinds().count("field_ref")
        assert reads == 1 if wait_s == 0 else reads > 1

    async def test_the_wait_leaves_the_probes_after_it_their_budget(self) -> None:
        # Every read during the wait hangs: the wait spends its own share and
        # no more, and the focus is still read afterwards.
        cdp = ProbeCDP(field=["null", "hang"])
        started = time.monotonic()
        effect = refused(
            await type_at(cdp, action_effect_timeout_s=0.6, type_focus_wait_s=0.2),
            NOTHING_FOCUSED,
        )
        assert time.monotonic() - started < 0.6
        assert (effect.target, effect.focus_after) == ("nothing", "nothing")

    async def test_a_reading_that_cannot_say_where_focus_is_leaves_the_last_one_standing(
        self,
    ) -> None:
        # Nothing focused, then reads of the focus that fail: the verdict and
        # the effect both rest on the last reading that said where focus is.
        cdp = ProbeCDP(field=["null", "fail"])
        effect = refused(await type_at(cdp, type_focus_wait_s=0.15), NOTHING_FOCUSED)
        assert (effect.target, effect.sensitive, effect.value_changed) == (
            "nothing",
            False,
            "unavailable",
        )
        assert cdp.kinds().count("field_ref") > 1
        assert keys_and_text(cdp) == []

    @pytest.mark.parametrize("late", ["field_read", "describe"], ids=["reading", "shadow roots"])
    async def test_the_end_of_the_wait_does_not_cut_short_reading_the_element_found(
        self, late: str
    ) -> None:
        # A button keeps focus, and the page answers the reading of it that a
        # poll took only after the wait is over. That reading still decides:
        # cut short, the button would be an element that cannot be told, and
        # typed into.
        cdp = ProbeCDP(
            field="node",
            readings=[reading("button", "Search", digest="6:1", tag="BUTTON", entry="no-role")],
            answer_after_s={late: [0.0, 0.3, 0.0]},
        )
        effect = refused(
            await type_at(cdp, type_focus_wait_s=0.1),
            'No text field took focus at the clicked point: focus is on button "Search", which '
            "does not take typed text, so no text was typed.",
        )
        assert effect.target == ElementFacts(role="button", name="Search")
        assert cdp.kinds().count("field_ref") == 2
        assert keys_and_text(cdp) == []

    async def test_a_focused_element_that_cannot_be_read_is_typed_into(self) -> None:
        # Nothing focused, then an element takes focus but its reading fails:
        # whether it takes text cannot be told, so the text is typed as asked.
        cdp = ProbeCDP(field=["null", "node"], readings=name_field("0:1"), fail={"field_read"})
        effect = text_input(await type_at(cdp))
        assert effect.target == "unknown"
        assert keys_and_text(cdp) == ["Input.dispatchKeyEvent"] * 6 + ["Input.insertText"]

    @pytest.mark.parametrize(
        ("field", "readings", "shadow_root", "fail"),
        [
            # Focus inside a frame of another site: it cannot be looked into.
            ("opaque", [], None, ()),
            # Embedded content keeps its own focus.
            ("node", [reading("generic", "", digest=None, tag="EMBED", entry=None)], None, ()),
            # A host whose shadow root is closed: the field may be inside it.
            (
                "node",
                [reading("generic", "", digest=None, tag="DIV", entry="no-role")],
                "closed",
                (),
            ),
            # The DOM agent did not say whether there is one.
            (
                "node",
                [reading("generic", "", digest=None, tag="DIV", entry="no-role")],
                None,
                ("describe",),
            ),
        ],
        ids=[
            "frame of another site",
            "embedded content",
            "closed shadow root",
            "shadow roots unknown",
        ],
    )
    async def test_a_reading_that_cannot_tell_types_as_asked(
        self,
        field: str,
        readings: list[dict[str, Any]],
        shadow_root: str | None,
        fail: tuple[str, ...],
    ) -> None:
        cdp = ProbeCDP(field=field, readings=readings, shadow_root=shadow_root, fail=fail)
        text_input(await type_at(cdp))
        assert keys_and_text(cdp) == ["Input.dispatchKeyEvent"] * 6 + ["Input.insertText"]

    async def test_an_open_shadow_root_hides_no_field(self) -> None:
        # Focus inside an open shadow root is followed by the page script; a
        # host that has focus itself is no text field.
        cdp = ProbeCDP(
            field="node",
            readings=[reading("generic", "Card", digest=None, tag="X-CARD", entry="no-role")],
            shadow_root="open",
        )
        refused(
            await type_at(cdp),
            'No text field took focus at the clicked point: focus is on generic "Card", which '
            "does not take typed text, so no text was typed.",
        )

    async def test_a_textbox_widget_gets_the_keys_without_the_page_select_all(self) -> None:
        # A role=textbox element that is no editor: the clear's keys reach it
        # for it to handle, without Chrome's select-all command, which would
        # select the whole page.
        cdp = ProbeCDP(
            field="node",
            readings=[reading("textbox", "Custom", digest=None, tag="DIV", entry="widget")],
        )
        effect = text_input(await type_at(cdp))
        assert effect.keys == ("Control+A", "Backspace")
        key_events = [c for c in cdp.inputs() if c.method == "Input.dispatchKeyEvent"]
        assert [c.params["key"] for c in key_events] == [
            "Control",
            "a",
            "a",
            "Control",
            "Backspace",
            "Backspace",
        ]
        assert all("commands" not in c.params for c in key_events)

    async def test_a_text_field_gets_the_select_all_command(self) -> None:
        cdp = ProbeCDP(field="node", readings=name_field("0:1", "3:2"))
        text_input(await type_at(cdp))
        select_all = next(
            c
            for c in cdp.inputs()
            if c.params.get("key") == "a" and c.params.get("type") == "keyDown"
        )
        assert select_all.params["commands"] == ["selectAll"]

    async def test_a_refused_coordinate_type_still_settles_after_its_click(self) -> None:
        settled: list[bool] = []
        action = TypeCoordinateAction(post_delay_ms=0)

        async def settle() -> None:
            settled.append(True)

        action.settle = settle  # type: ignore[method-assign]
        async with effect_tab(ProbeCDP(field="null")) as tab:
            result = await action.execute(
                {"coordinate_x": 100, "coordinate_y": 150, "text": "Ada"},
                capability=tab,  # type: ignore[arg-type]
            )
        refused(result, NOTHING_FOCUSED)
        assert settled == [True]

    async def test_a_coordinate_type_whose_click_failed_does_not_settle(self) -> None:
        settled: list[bool] = []
        action = TypeCoordinateAction(post_delay_ms=0)

        async def settle() -> None:
            settled.append(True)

        action.settle = settle  # type: ignore[method-assign]
        async with effect_tab(ProbeCDP(fail_methods={"Input.dispatchMouseEvent"})) as tab:
            result = await action.execute(
                {"coordinate_x": 100, "coordinate_y": 150, "text": "Ada"},
                capability=tab,  # type: ignore[arg-type]
            )
        assert (result.success, result.effect) == (False, None)
        assert settled == []


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
