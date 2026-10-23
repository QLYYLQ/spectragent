"""The UI-TARS codec: one declaration per verb reads and writes the protocol, with no state.

``spectragent/agents/ui_tars/codec.py``. A :class:`UITarsCodec` is built from a
run's action space and reads only its arguments and itself, so:

- every verb writes an action and reads the text back into the same action,
  and the written text is a fixed point;
- a verb's point arguments fill the points its action declares, under the
  action's own param names (never names this module restates);
- a malformed reply or an unwritable action is refused with the same words
  as before the codec existed (the agent's tests pin the rest of them);
- two codecs for one space say the same thing, in any call order.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.ui_tars import UITarsAgent
from spectragent.agents.ui_tars.codec import VERBS, UITarsCodec, read_thought
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.loops.coordinate_frames import (
    actions_in_capability_units,
    actions_in_model_frame,
    step_frame,
)
from spectragent.core.loops.errors import ParseError, UnrepresentableActionError
from spectragent.schemas.action import Action, ActionDescriptor
from tests._helpers.action_spaces import browser_descriptors, space_of, wired_space

#: One value per param a verb fills; the points lie on a 1280x720 viewport.
_VALUES: dict[str, Any] = {
    "coordinate_x": 640,
    "coordinate_y": 360,
    "start_x": 1,
    "start_y": 2,
    "end_x": 999,
    "end_y": 700,
    "text": 'it\'s "done"\n東京 \\ x',
    "direction": "left",
    "keys": "Control+Shift+t",
    "extracted_content": "the answer",
}


_SCREENSHOT = ScreenshotObs(
    image_bytes=b"\x89PNG\r\n\x1a\n",
    image_width=1280,
    image_height=720,
    viewport_width=1280,
    viewport_height=720,
)


def _codec(**kwargs: Any) -> UITarsCodec:
    return UITarsCodec(wired_space(UITarsAgent), **kwargs)


def _action(codec: UITarsCodec, verb: str) -> Action:
    """An action the verb writes, under the run's full name, every param it fills set."""
    bound = codec.verbs[verb]
    return Action(name=bound.descriptor.name, params={key: _VALUES[key] for key in bound.params})


class TestEveryVerbRoundTrips:
    @pytest.mark.parametrize("verb", list(VERBS))
    def test_write_then_read_is_the_same_action(self, verb: str) -> None:
        codec = _codec()
        action = _action(codec, verb)
        (back,) = codec.read_actions(codec.write([action]))
        assert back.name == VERBS[verb].action
        assert back.params == action.params

    @pytest.mark.parametrize("verb", list(VERBS))
    def test_the_written_text_is_a_fixed_point(self, verb: str) -> None:
        """As the training export holds it (arch §4.15 "round-trip", clause 2).

        A recorded action's points go onto the grid, the reply is read and its
        points come back into viewport units, and writing that again gives the
        same text. The framework moves the points; the codec only spells them.
        """
        space = wired_space(UITarsAgent, coordinate_space="normalized_1000")
        codec = UITarsCodec(space)
        frame = step_frame(space, SimpleNamespace(screenshot=_SCREENSHOT))
        recorded = [_action(codec, verb)]
        text = codec.write(actions_in_model_frame(recorded, frame, space), thought="a  b\nc")
        assert text.startswith("Thought: a b c\nAction: ")
        read = actions_in_capability_units(codec.read_actions(text), frame, space)
        assert codec.write(actions_in_model_frame(read, frame, space), thought="a b c") == text

    def test_a_reply_of_every_verb_reads_back_in_order(self) -> None:
        codec = _codec()
        actions = [_action(codec, verb) for verb in VERBS]
        back = codec.read_actions(codec.write(actions))
        assert [a.name for a in back] == [verb.action for verb in VERBS.values()]
        assert [a.params for a in back] == [a.params for a in actions]

    def test_each_verb_is_written_as_its_signature_teaches(self) -> None:
        codec = _codec()
        written = {verb: codec.write_call(_action(codec, verb)) for verb in VERBS}
        assert written == {
            "click": "click(point='(640, 360)')",
            "type": "type(content='it\\'s \"done\"\\n東京 \\\\ x', point='(640, 360)')",
            "scroll": "scroll(direction='left')",
            "hotkey": "hotkey(key='Control+Shift+t')",
            "drag": "drag(start_point='(1, 2)', end_point='(999, 700)')",
            "wait": "wait()",
            "finished": "finished(content='the answer')",
        }

    @pytest.mark.parametrize(
        ("reply", "params"),
        [
            ("click(start_box='(640, 360)')", {"coordinate_x": 640, "coordinate_y": 360}),
            (
                "drag(start_box='(1, 2)', end_box='(999, 1000)')",
                {"start_x": 1, "start_y": 2, "end_x": 999, "end_y": 1000},
            ),
            ("hotkey(keys='Control+Shift+t')", {"keys": "Control+Shift+t"}),
        ],
    )
    def test_the_other_spellings_read_as_the_taught_one(
        self, reply: str, params: dict[str, Any]
    ) -> None:
        (action,) = _codec().read_actions(f"Action: {reply}")
        assert action.params == params

    def test_the_grounding_template_writes_no_thought(self) -> None:
        codec = _codec(writes_thought=False)
        assert codec.write([_action(codec, "wait")], thought="why") == "Action: wait()"


class TestPointsAreTheActionsOwn:
    def test_a_point_fills_the_params_the_action_declares(self) -> None:
        """The table names no point param: a desktop click with its own names gets them."""
        click = ActionDescriptor(
            name="desktop.click_coordinate",
            description="Click at a screen point.",
            params={"x": "int", "y": "int"},
            point_params=(("x", "y"),),
        )
        codec = UITarsCodec(space_of([click]))
        (action,) = codec.read_actions("Action: click(point='(3, 4)')")
        assert action == Action(name="click_coordinate", params={"x": 3, "y": 4})
        assert codec.write_call(
            Action(name="desktop.click_coordinate", params={"x": 5, "y": 6})
        ) == ("click(point='(5, 6)')")

    def test_a_verb_and_an_action_that_disagree_on_points_are_refused_when_built(self) -> None:
        """No point params to fill: the run would execute a grid point as capability units."""
        bare = ActionDescriptor(name="browser.click_coordinate", description="Click.")
        with pytest.raises(ValueError, match=r"click\(\) writes 1 point\(s\)") as excinfo:
            UITarsCodec(space_of([bare]))
        assert "browser.click_coordinate" in str(excinfo.value)
        assert "declares 0" in str(excinfo.value)

    def test_a_verb_whose_action_the_run_lacks_is_not_offered(self) -> None:
        narrow = UITarsCodec(space_of(browser_descriptors("wait")))
        assert list(narrow.verbs) == ["wait"]
        assert narrow.example_call() == "wait()"


class TestMalformedRepliesKeepTheirWords:
    @pytest.mark.parametrize(
        ("reply", "detail"),
        [
            (
                "",
                "The reply was empty. It must contain at least one line that starts with "
                "'Action:', e.g. Action: click(point='(x, y)').",
            ),
            (
                "Thought: done. Action: click(point='(1, 2)')",
                "No line starts with 'Action:'. 'Action:' does appear, but not at the start "
                "of a line, so it was read as prose. Put each action on its own line, e.g. "
                "Action: click(point='(x, y)').",
            ),
            (
                "Action: zoom(point='(1, 2)')",
                "Action line 1: unknown verb 'zoom'; this run offers: click, type, scroll, "
                "hotkey, drag, wait, finished",
            ),
            (
                "Action: click('(1, 2)')",
                "Action line 1, click('(1, 2)'): pass arguments by name, as in "
                "click(point='(x, y)')",
            ),
            (
                "Action: click(point='(1, 2)', button='right')",
                "Action line 1, click(point='(1, 2)', button='right'): click() does not take "
                "button; write it as click(point='(x, y)')",
            ),
            ("Action: click()", "Action line 1, click(): a point is missing; write point='(x, y)'"),
            (
                "Action: click(point='(1, 2, 3)')",
                "Action line 1, click(point='(1, 2, 3)'): point='(1, 2, 3)' has 3 numbers; "
                "write two for a point (point='(x, y)') or four for a box "
                "(point='(x1, y1, x2, y2)')",
            ),
            (
                "Action: click(point='btn2')",
                "Action line 1, click(point='btn2'): point='btn2' is not a coordinate; expected "
                "point='(x, y)' — two numbers and nothing else, e.g. point='(640, 360)'. "
                "Describe nothing there: no element name, selector or text, only the numbers",
            ),
            (
                "Action: click(point='(1, 2)', start_box='(3, 4)')",
                "Action line 1, click(point='(1, 2)', start_box='(3, 4)'): point and start_box "
                "say the same thing; write only point",
            ),
            (
                "Action: type(point='(1, 2)')",
                "Action line 1, type(point='(1, 2)'): type needs content='text to type'",
            ),
            (
                "Action: type(content='x')",
                "Action line 1, type(content='x'): a point is missing; write point='(x, y)'",
            ),
            (
                "Action: scroll(direction='sideways')",
                "Action line 1, scroll(direction='sideways'): scroll needs "
                "direction='up'|'down'|'left'|'right'",
            ),
            ("Action: hotkey(key='')", "Action line 1, hotkey(key=''): hotkey needs key='ctrl+a'"),
            (
                "Action: drag(start_point='(1, 2)')",
                "Action line 1, drag(start_point='(1, 2)'): a end_point is missing; write "
                "end_point='(x, y)'",
            ),
            (
                "Action: wait(seconds=3)",
                "Action line 1, wait(seconds=3): wait() does not take seconds; write it as wait()",
            ),
        ],
    )
    def test_the_detail_is_the_one_the_model_was_always_given(
        self, reply: str, detail: str
    ) -> None:
        with pytest.raises(ParseError) as excinfo:
            _codec().read_actions(reply)
        assert excinfo.value.detail == detail
        assert excinfo.value.raw_output == reply[:500]

    def test_a_codec_without_verbs_offers_none(self) -> None:
        with pytest.raises(ParseError) as excinfo:
            UITarsCodec().read_actions("Action: wait()")
        assert excinfo.value.detail == "Action line 1: unknown verb 'wait'; this run offers: none"


class TestUnwritableActionsAreNamed:
    @pytest.mark.parametrize(
        ("action", "params", "detail"),
        [
            (
                Action(name="browser.navigate", params={"url": "https://x.test"}),
                [],
                "this run does not execute it (click() runs as browser.click_coordinate; "
                "type() runs as browser.type_coordinate; scroll() runs as browser.scroll; "
                "hotkey() runs as browser.send_keys; drag() runs as browser.drag; wait() runs "
                "as browser.wait; finished() runs as browser.done)",
            ),
            (
                Action(name="browser.done", params={"extracted_content": "y", "success": True}),
                ["success"],
                "finished(content='answer if applicable') has no argument that carries it",
            ),
            (
                Action(name="browser.scroll", params={"direction": "down", "amount": 300}),
                ["amount"],
                "scroll(direction='up|down|left|right') has no argument that carries it",
            ),
            (
                Action(
                    name="browser.click_coordinate",
                    params={"coordinate_x": "outside the screenshot", "coordinate_y": 7},
                ),
                ["coordinate_x", "coordinate_y"],
                "('outside the screenshot', 7) is not a point",
            ),
            (
                Action(name="browser.send_keys", params={"keys": "ctrl c"}),
                ["keys"],
                "keys='ctrl c' is empty or holds whitespace, which hotkey() reads as a chord",
            ),
            (
                Action(name="browser.scroll", params={"direction": "UP"}),
                ["direction"],
                "direction='UP' is not one of ('up', 'down', 'left', 'right')",
            ),
            (
                Action(
                    name="browser.type_coordinate", params={"coordinate_x": 1, "coordinate_y": 2}
                ),
                ["text"],
                "text has no value to write",
            ),
        ],
    )
    def test_the_refusal_names_the_action_and_its_params(
        self, action: Action, params: list[str], detail: str
    ) -> None:
        codec = _codec()
        with pytest.raises(UnrepresentableActionError) as excinfo:
            codec.write([action])
        assert excinfo.value.action_name == action.name
        assert list(excinfo.value.params) == params
        assert excinfo.value.detail == detail
        # Shown as a fact, an unwritable action keeps its JSON.
        assert codec.write_fact(action) == action.model_dump(mode="json")

    def test_no_text_and_empty_text_are_both_written_as_no_argument(self) -> None:
        codec = _codec()
        for content in (None, ""):
            done = Action(name="browser.done", params={"extracted_content": content})
            assert codec.write_call(done) == "finished()"

    def test_no_reply_carries_no_action(self) -> None:
        with pytest.raises(ValueError, match="at least one action"):
            _codec().write([])


class TestNoState:
    def test_the_codec_is_immutable(self) -> None:
        codec = _codec()
        with pytest.raises(dataclasses.FrozenInstanceError):
            codec.writes_thought = False  # type: ignore[misc]
        with pytest.raises(TypeError):
            codec.verbs["zoom"] = codec.verbs["click"]  # type: ignore[index]

    def test_two_codecs_for_one_space_agree_in_any_order(self) -> None:
        first, second = _codec(), _codec()
        actions = [_action(first, verb) for verb in VERBS]
        reply = first.write(actions)
        assert second.read_actions(reply) == first.read_actions(reply)
        assert [second.write_fact(a) for a in reversed(actions)] == [
            first.write_fact(a) for a in reversed(actions)
        ]

    def test_the_thought_reads_without_a_run(self) -> None:
        reply = "**Thought:** the box\nis here\nAction: click(point='(1, 2)')"
        assert read_thought(reply) == ("the box\nis here", "Thought")
        assert read_thought("Action: wait()") is None
        assert UITarsAgent.parse_react_thought(reply) == read_thought(reply)
