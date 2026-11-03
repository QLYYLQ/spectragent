"""The UI-TARS codec: one declaration per verb reads and writes the protocol, with no state.

``spectragent/agents/ui_tars/codec.py``. A :class:`UITarsCodec` is built from a
run's action space and reads only its arguments and itself, so:

- every verb writes an action and reads the text back into the same action,
  and the written text is a fixed point;
- a verb's point arguments fill the points its action declares, under the
  action's own param names (never names this module restates), required with
  required and optional with optional;
- a malformed reply or an unwritable action is refused with the same words
  as before the codec existed (the agent's tests pin the rest of them);
- a verb binds only where the run holds its action: the mobile verbs bind to
  the Android capability's actions and to nothing in a browser run;
  ``press_home`` and ``press_back`` run as one action, told apart by the key
  each fixes;
- two codecs for one space say the same thing, in any call order.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict

from spectragent.agents.ui_tars import UITarsAgent
from spectragent.agents.ui_tars.codec import (
    VERBS,
    Argument,
    BoundVerb,
    UITarsCodec,
    Verb,
    read_thought,
)
from spectragent.core.action_guard import params_refusal
from spectragent.core.actions.params import PointAxis, params_view, point_params
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.loops.coordinate_frames import (
    actions_in_capability_units,
    actions_in_model_frame,
    step_frame,
)
from spectragent.core.loops.errors import ParseError, UnrepresentableActionError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.negotiation import extract_wiring
from spectragent.schemas.action import Action, ActionDescriptor
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.geometry import CoordinateSpace
from tests._helpers.action_spaces import browser_descriptors, space_for, space_of, wired_space
from tests._helpers.android_device import unset_capability

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
    "app_name": "Clock",
}

#: The verbs that run as Android's actions, which a browser run does not hold.
_MOBILE_VERBS = ("long_press", "open_app", "press_home", "press_back")
#: The actions those verbs run as, which only Android has.
_ANDROID_ONLY = ("long_press", "open_app", "press_key")


_SCREENSHOT = ScreenshotObs(
    image_bytes=b"\x89PNG\r\n\x1a\n",
    image_width=1280,
    image_height=720,
    viewport_width=1280,
    viewport_height=720,
)


def _codec(**kwargs: Any) -> UITarsCodec:
    """A codec for UI-TARS's space, framed on its grid as a run frames it."""
    return UITarsCodec(wired_space(UITarsAgent, coordinate_space="screenshot_pixels"), **kwargs)


def _android_codec() -> UITarsCodec:
    """The codec of a UI-TARS run on the Android capability, which holds all its actions."""
    return UITarsCodec(space_for(UITarsAgent(), unset_capability().get_action_descriptors()))


def _every_verb_space(coordinate_space: CoordinateSpace = "screenshot_pixels") -> ActionSpace:
    """A space holding an action for every verb: the browser's, and Android's where a browser has none.

    The browser actions UI-TARS's wiring names, and the Android capability's
    ``long_press``, ``open_app`` and ``press_key``. One action per relative
    name: a name two actions share binds to neither.
    """
    required, optional = extract_wiring(UITarsAgent, "action")
    android = {d.name: d for d in unset_capability().get_action_descriptors()}
    return space_of(
        [
            *browser_descriptors(*sorted((required | optional) - set(_ANDROID_ONLY))),
            *(android[f"android.{name}"] for name in _ANDROID_ONLY),
        ],
        coordinate_space=coordinate_space,
    )


def _every_verb_codec(**kwargs: Any) -> UITarsCodec:
    return UITarsCodec(_every_verb_space(), **kwargs)


class _PanParams(BaseModel):
    """An action with an optional point: given, the action happens there."""

    model_config = ConfigDict(extra="forbid")

    direction: Literal["up", "down"]
    at_x: Annotated[int | None, PointAxis("at", "x")] = None
    at_y: Annotated[int | None, PointAxis("at", "y")] = None


def _pan() -> ActionDescriptor:
    prose, schema = params_view(_PanParams)
    return ActionDescriptor(
        name="desktop.pan",
        description="Pan.",
        params=prose,
        params_schema=schema,
        point_params=tuple(point_params(_PanParams)),
    )


#: A verb whose point is optional, as UI-TARS writes a scroll's start.
_PAN_VERB = Verb(
    "pan",
    "pan",
    (
        Argument("start_box", "point", "'(x, y)'", required=False),
        Argument("direction", "choice", "'up|down'", param="direction", choices=("up", "down")),
    ),
)


def _action(codec: UITarsCodec, verb: str) -> Action:
    """An action the verb writes, under the run's full name, every param it fills set."""
    bound = codec.verbs[verb]
    values = {**_VALUES, **bound.verb.fixed}
    return Action(name=bound.descriptor.name, params={key: values[key] for key in bound.params})


class TestEveryVerbRoundTrips:
    @pytest.mark.parametrize("verb", list(VERBS))
    def test_write_then_read_is_the_same_action(self, verb: str) -> None:
        codec = _every_verb_codec()
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
        space = _every_verb_space("normalized_1000")
        codec = UITarsCodec(space)
        frame = step_frame(space, SimpleNamespace(screenshot=_SCREENSHOT))
        recorded = [_action(codec, verb)]
        text = codec.write(actions_in_model_frame(recorded, frame, space), thought="a  b\nc")
        assert text.startswith("Thought: a b c\nAction: ")
        read = actions_in_capability_units(codec.read_actions(text), frame, space)
        assert codec.write(actions_in_model_frame(read, frame, space), thought="a b c") == text

    def test_a_reply_of_every_verb_reads_back_in_order(self) -> None:
        codec = _every_verb_codec()
        actions = [_action(codec, verb) for verb in VERBS]
        back = codec.read_actions(codec.write(actions))
        assert [a.name for a in back] == [verb.action for verb in VERBS.values()]
        assert [a.params for a in back] == [a.params for a in actions]

    def test_each_verb_is_written_as_its_signature_teaches(self) -> None:
        codec = _every_verb_codec()
        written = {verb: codec.write_call(_action(codec, verb)) for verb in VERBS}
        assert written == {
            "click": "click(point='(640, 360)')",
            "long_press": "long_press(point='(640, 360)')",
            "type": "type(content='it\\'s \"done\"\\n東京 \\\\ x', point='(640, 360)')",
            "scroll": "scroll(start_box='(1, 2)', direction='left')",
            "open_app": "open_app(app_name='Clock')",
            "hotkey": "hotkey(key='Control+Shift+t')",
            "drag": "drag(start_point='(1, 2)', end_point='(999, 700)')",
            "press_home": "press_home()",
            "press_back": "press_back()",
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
            (
                "scroll(point='(5, 6)', direction='down')",
                {"start_x": 5, "start_y": 6, "direction": "down"},
            ),
        ],
    )
    def test_the_other_spellings_read_as_the_taught_one(
        self, reply: str, params: dict[str, Any]
    ) -> None:
        (action,) = _codec().read_actions(f"Action: {reply}")
        assert action.params == params

    def test_a_scroll_without_a_start_is_written_without_one(self) -> None:
        """The start is optional both ways: an action without one has no start_box."""
        codec = _codec()
        text = codec.write_call(Action(name="browser.scroll", params={"direction": "down"}))
        assert text == "scroll(direction='down')"
        (back,) = codec.read_actions(f"Action: {text}")
        assert back.params == {"direction": "down"}

    @pytest.mark.parametrize("make", [_codec, _android_codec], ids=["browser", "android"])
    def test_a_type_without_a_point_is_read_and_written_without_one(
        self, make: Callable[[], UITarsCodec]
    ) -> None:
        """UI-TARS's own ``type(content=...)``: no point, so into the field that has focus."""
        codec = make()
        (typed,) = codec.read_actions("Action: type(content='hello')")
        assert typed == Action(name="type_coordinate", params={"text": "hello"})
        full = codec.verbs["type"].descriptor.name
        assert codec.write_call(typed.model_copy(update={"name": full})) == (
            "type(content='hello')"
        )

    @pytest.mark.parametrize("spelling", ["point", "start_box"])
    def test_a_type_with_a_point_keeps_it(self, spelling: str) -> None:
        codec = _codec()
        params = {"coordinate_x": 3, "coordinate_y": 4, "text": "hello"}
        (typed,) = codec.read_actions(f"Action: type(content='hello', {spelling}='(3, 4)')")
        assert typed.params == params
        assert codec.write_call(Action(name="browser.type_coordinate", params=params)) == (
            "type(content='hello', point='(3, 4)')"
        )

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

    def test_an_optional_point_is_read_and_written_only_when_given(self) -> None:
        bound = BoundVerb.bind(_PAN_VERB, _pan())
        assert bound.read({"start_box": "(3, 4)", "direction": "up"}) == {
            "at_x": 3,
            "at_y": 4,
            "direction": "up",
        }
        assert bound.read({"direction": "up"}) == {"direction": "up"}
        assert bound.write({"at_x": 3, "at_y": 4, "direction": "up"}) == (
            "pan(start_box='(3, 4)', direction='up')"
        )
        assert bound.write({"direction": "up"}) == "pan(direction='up')"

    def test_required_and_optional_points_are_counted_apart(self) -> None:
        """A required point the verb may leave out, or an optional one it must write, is refused."""
        required = _pan().model_copy(
            update={"params_schema": {**(_pan().params_schema or {}), "required": ["at_x", "at_y"]}}
        )
        with pytest.raises(ValueError, match=r"pan\(\) writes 0 point\(s\) and 1 optional") as info:
            BoundVerb.bind(_PAN_VERB, required)
        assert "declares 1 and 0 optional" in str(info.value)
        writes_one = dataclasses.replace(
            _PAN_VERB,
            arguments=(dataclasses.replace(_PAN_VERB.arguments[0], required=True),),
        )
        with pytest.raises(ValueError, match=r"writes 1 point\(s\) and 0 optional"):
            BoundVerb.bind(writes_one, _pan())

    @pytest.mark.parametrize("make", [_codec, _android_codec], ids=["browser", "android"])
    def test_types_optional_point_fills_the_actions_optional_point(
        self, make: Callable[[], UITarsCodec]
    ) -> None:
        """On both capabilities, so the agent's check in ``prepare()`` passes on both."""
        bound = make().verbs["type"]
        ((argument, pair),) = bound.points
        assert (argument.name, argument.required) == ("point", False)
        assert pair == ("coordinate_x", "coordinate_y")
        assert bound.descriptor.optional_points == (pair,)

    def test_a_verb_whose_action_the_run_lacks_is_not_offered(self) -> None:
        narrow = UITarsCodec(space_of(browser_descriptors("wait")))
        assert list(narrow.verbs) == ["wait"]
        assert narrow.example_call() == "wait()"

    def test_the_agent_refuses_the_same_space_when_the_runtime_asks(self) -> None:
        """``AgentRuntime.prepare()`` asks before the run starts (``ActionSpaceCheck``)."""
        bare = ActionDescriptor(name="browser.click_coordinate", description="Click.")
        with pytest.raises(ValueError, match=r"click\(\) writes 1 point\(s\)"):
            UITarsAgent().check_action_space(space_of([bare]))
        UITarsAgent().check_action_space(_every_verb_space())


class _KeysWithoutHome(BaseModel):
    """A ``press_key`` whose keys leave out ``home``."""

    model_config = ConfigDict(extra="forbid")

    key: Literal["back", "enter"]


class _KeycodeOnly(BaseModel):
    """A ``press_key`` with no ``key`` param at all."""

    model_config = ConfigDict(extra="forbid")

    keycode: int


def _press_key(params: type[BaseModel]) -> ActionDescriptor:
    prose, schema = params_view(params)
    return ActionDescriptor(
        name="android.press_key",
        description="Press a system key.",
        params=prose,
        params_schema=schema,
    )


class TestMobileVerbs:
    """``long_press``, ``open_app``, ``press_home`` and ``press_back`` run as Android's actions."""

    def test_a_browser_run_binds_none_of_them(self) -> None:
        assert not set(_MOBILE_VERBS) & set(_codec().verbs)

    def test_a_run_holding_androids_actions_binds_each_to_its_action(self) -> None:
        verbs = _android_codec().verbs
        assert {verb: verbs[verb].descriptor.name for verb in _MOBILE_VERBS} == {
            "long_press": "android.long_press",
            "open_app": "android.open_app",
            "press_home": "android.press_key",
            "press_back": "android.press_key",
        }

    def test_long_press_holds_where_its_point_is(self) -> None:
        codec = _android_codec()
        held = Action(name="long_press", params={"coordinate_x": 10, "coordinate_y": 20})
        assert codec.read_actions("Action: long_press(point='(10, 20)')") == [held]
        # UI-TARS's mobile prompt spells the point start_box.
        assert codec.read_actions("Action: long_press(start_box='(10, 20)')") == [held]
        assert codec.write_call(held.model_copy(update={"name": "android.long_press"})) == (
            "long_press(point='(10, 20)')"
        )

    def test_a_long_press_without_a_point_is_refused_by_the_actions_params(self) -> None:
        """The point is optional on the action (an element index is its other target, which
        a vision run lacks), so the verb's is too, and the run's params check refuses neither."""
        codec = _android_codec()
        (bare,) = codec.read_actions("Action: long_press()")
        assert bare == Action(name="long_press", params={})
        refusal = params_refusal(codec.space, bare)
        assert refusal is not None and "long_press needs a point or an element index" in refusal

    def test_open_app_names_the_app(self) -> None:
        codec = _android_codec()
        opened = Action(name="open_app", params={"app_name": "Clock"})
        assert codec.read_actions("Action: open_app(app_name='Clock')") == [opened]
        assert codec.write_call(opened) == "open_app(app_name='Clock')"
        with pytest.raises(ParseError, match="open_app needs app_name='app name'"):
            codec.read_actions("Action: open_app()")

    @pytest.mark.parametrize(("verb", "key"), [("press_home", "home"), ("press_back", "back")])
    def test_a_key_verb_presses_the_key_it_fixes(self, verb: str, key: str) -> None:
        codec = _android_codec()
        assert codec.verbs[verb].verb.signature == f"{verb}()"
        assert codec.read_actions(f"Action: {verb}()") == [
            Action(name="press_key", params={"key": key})
        ]
        pressed = Action(name="android.press_key", params={"key": key})
        assert codec.write_call(pressed) == f"{verb}()"

    def test_the_fixed_key_is_no_argument_of_the_call(self) -> None:
        with pytest.raises(ParseError) as excinfo:
            _android_codec().read_actions("Action: press_home(key='back')")
        assert excinfo.value.detail == (
            "Action line 1, press_home(key='back'): press_home() does not take key; "
            "write it as press_home()"
        )

    def test_a_key_no_verb_fixes_cannot_be_written(self) -> None:
        codec = _android_codec()
        enter = Action(name="android.press_key", params={"key": "enter"})
        with pytest.raises(UnrepresentableActionError) as excinfo:
            codec.write_call(enter)
        assert list(excinfo.value.params) == ["key"]
        assert excinfo.value.detail == (
            "no UI-TARS verb sets key='enter' (press_home() sets key='home'; "
            "press_back() sets key='back')"
        )
        # Shown as a fact, it keeps its JSON.
        assert codec.write_fact(enter) == enter.model_dump(mode="json")

    def test_a_verb_whose_fixed_value_the_action_refuses_is_not_offered(self) -> None:
        """A ``press_key`` without ``home``: ``press_home`` is unbound, untaught, an unknown verb."""
        space = space_of([_press_key(_KeysWithoutHome)])
        codec = UITarsCodec(space)
        assert list(codec.verbs) == ["press_back"]
        # No key param at all: neither key verb can set one.
        assert not UITarsCodec(space_of([_press_key(_KeycodeOnly)])).verbs
        with pytest.raises(ParseError) as excinfo:
            codec.read_actions("Action: press_home()")
        assert excinfo.value.detail == (
            "Action line 1: unknown verb 'press_home'; this run offers: press_back"
        )
        prompt = (
            UITarsAgent()
            .prepare(
                task="t",
                action_space=space,
                execution_limits=ExecutionLimits(max_actions_per_step=1),
            )[0]
            .content
        )
        assert isinstance(prompt, str)
        assert "- press_back()" in prompt
        assert "press_home" not in prompt

    def test_a_bound_verb_writes_only_the_value_it_fixes(self) -> None:
        bound = _android_codec().verbs["press_home"]
        assert bound.write({"key": "home"}) == "press_home()"
        with pytest.raises(ValueError, match=r"press_home\(\) always sets key='home'"):
            bound.write({"key": "back"})


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
                "as browser.wait; finished() runs as loop.done)",
            ),
            (
                Action(name="loop.done", params={"extracted_content": "y", "success": True}),
                ["success"],
                "finished(content='answer if applicable') has no argument that carries it",
            ),
            (
                Action(name="browser.scroll", params={"direction": "down", "amount": 300}),
                ["amount"],
                "scroll(start_box='(x, y)', direction='up|down|left|right') has no argument "
                "that carries it",
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
            done = Action(name="loop.done", params={"extracted_content": content})
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
        first, second = _every_verb_codec(), _every_verb_codec()
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
