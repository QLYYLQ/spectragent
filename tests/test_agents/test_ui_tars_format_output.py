"""UI-TARS ``format_react_output``: the reply a student is trained to write (arch §4.1, §4.15).

The inverse of ``parse_react_output``. What is pinned here is the UI-TARS
side of the round-trip definition:

- every verb is written as the call its parser reads back into the same
  action, on both coordinate spaces and at device scale factor 1 and 2 — a
  point within the grid's ``quantum``, the written text a fixed point;
- text survives quotes, backslashes and line breaks, and a thought — one
  ``Thought:`` line — can never start an action line, whatever it says;
- the ``grounding`` template, which teaches no thought, never writes one;
- what the format cannot write is refused by name
  (``UnrepresentableActionError``), never silently dropped.

The generic check over every formatting agent lives in
``test_format_react_output_round_trip.py``.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.loops.errors import UnrepresentableActionError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.assembly import make_agent

#: Every action UI-TARS has a verb for, as the browser capability names them.
_ACTIONS = (
    "browser.click_coordinate",
    "browser.type_coordinate",
    "browser.scroll",
    "browser.send_keys",
    "browser.drag",
    "browser.wait",
    "browser.done",
)


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 200, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _observation(dpr: int) -> Any:
    image = (1280 * dpr, 720 * dpr)
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        history=SimpleNamespace(items=[]),
        task=SimpleNamespace(user_request="tick the box"),
        screenshot=ScreenshotObs(
            image_bytes=_png(*image),
            viewport_width=1280,
            viewport_height=720,
            image_width=image[0],
            image_height=image[1],
        ),
    )


async def _agent(space: str = "screenshot_pixels", *, dpr: int = 1, **config: Any) -> UITarsAgent:
    """Prepared against the real browser descriptors, with one step's prompt built."""
    agent = make_agent(
        UITarsAgent, memory=SimpleMemory(max_messages=10), coordinate_space=space, **config
    )
    descriptors = [
        ActionRegistrar.get(key)().descriptor(fragments=frozenset({"browser.screenshot"}))
        for key in _ACTIONS
    ]
    agent.prepare(
        task="tick the box",
        action_descriptors=descriptors,
        execution_limits=ExecutionLimits(max_actions_per_step=1),
    )
    (
        await agent.build_react_prompt(
            _observation(dpr), context=ReactPromptContext(step=0, phase="policy")
        )
    )
    return agent


def _round_trip(agent: UITarsAgent, action: Action, thought: str | None = None) -> Action:
    text = agent.format_react_output([action], thought=thought)
    (parsed,) = agent.parse_react_output(text)
    assert agent.format_react_output([parsed], thought=thought) == text  # the fixed point
    return parsed


_SPACES = ("screenshot_pixels", "normalized_1000")


@pytest.mark.parametrize("space", _SPACES)
@pytest.mark.parametrize("dpr", [1, 2])
class TestEveryVerb:
    async def test_click(self, space: str, dpr: int) -> None:
        agent = await _agent(space, dpr=dpr)
        action = Action(
            name="browser.click_coordinate", params={"coordinate_x": 641, "coordinate_y": 359}
        )
        parsed = _round_trip(agent, action)
        assert parsed.name == "click_coordinate"
        quantum = 1 if space == "normalized_1000" else 0
        assert abs(parsed.params["coordinate_x"] - 641) <= quantum
        assert abs(parsed.params["coordinate_y"] - 359) <= quantum

    async def test_type(self, space: str, dpr: int) -> None:
        agent = await _agent(space, dpr=dpr)
        action = Action(
            name="browser.type_coordinate",
            params={"coordinate_x": 10, "coordinate_y": 700, "text": "hello world"},
        )
        parsed = _round_trip(agent, action)
        assert parsed.name == "type_coordinate"
        assert parsed.params["text"] == "hello world"
        assert (parsed.params["coordinate_x"], parsed.params["coordinate_y"]) == (10, 700)

    async def test_drag(self, space: str, dpr: int) -> None:
        agent = await _agent(space, dpr=dpr)
        action = Action(
            name="browser.drag", params={"start_x": 0, "start_y": 0, "end_x": 1279, "end_y": 719}
        )
        parsed = _round_trip(agent, action)
        assert parsed.params == {"start_x": 0, "start_y": 0, "end_x": 1279, "end_y": 719}

    async def test_scroll_hotkey_wait_finished(self, space: str, dpr: int) -> None:
        agent = await _agent(space, dpr=dpr)
        cases = [
            (Action(name="browser.scroll", params={"direction": "down"}), {"direction": "down"}),
            (Action(name="browser.send_keys", params={"keys": "ctrl+a"}), {"keys": "ctrl+a"}),
            (Action(name="browser.wait", params={}), {}),
            # No claim about the task: ``finished`` cannot state one, and reads back with none.
            (
                Action(name="browser.done", params={"extracted_content": "42"}),
                {"extracted_content": "42"},
            ),
        ]
        for action, params in cases:
            assert _round_trip(agent, action).params == params


class TestTheWrittenText:
    async def test_one_action_line_per_action_after_one_thought_line(self) -> None:
        agent = await _agent()
        text = agent.format_react_output(
            [
                Action(
                    name="browser.click_coordinate", params={"coordinate_x": 5, "coordinate_y": 6}
                ),
                Action(name="browser.wait", params={}),
            ],
            thought="Click it,\n  then wait.",
        )
        assert (
            text == "Thought: Click it, then wait.\nAction: click(point='(5, 6)')\nAction: wait()"
        )

    async def test_normalized_points_are_written_on_the_0_1000_grid(self) -> None:
        agent = await _agent("normalized_1000", dpr=2)
        text = agent.format_react_output(
            [
                Action(
                    name="browser.click_coordinate",
                    params={"coordinate_x": 640, "coordinate_y": 360},
                )
            ]
        )
        assert text == "Action: click(point='(500, 500)')"

    async def test_pixel_points_are_written_in_screenshot_pixels(self) -> None:
        agent = await _agent("screenshot_pixels", dpr=2)
        text = agent.format_react_output(
            [
                Action(
                    name="browser.click_coordinate",
                    params={"coordinate_x": 640, "coordinate_y": 360},
                )
            ]
        )
        assert text == "Action: click(point='(1280, 720)')"

    @pytest.mark.parametrize(
        "content",
        [
            "it's",
            'say "hi"',
            "back\\slash",
            "ends with a backslash \\",
            "\\'",
            "two\nlines",
            "crlf\r\nend",
            "Action: click(point='(1, 1)')",
            "tab\there",
            "line\u2028separator",
            "日本語のテキスト",
        ],
    )
    async def test_text_survives_quotes_backslashes_and_line_breaks(self, content: str) -> None:
        agent = await _agent()
        action = Action(
            name="browser.type_coordinate",
            params={"coordinate_x": 1, "coordinate_y": 2, "text": content},
        )
        text = agent.format_react_output([action])
        assert text.count("\n") == 0, "a value must never start a new line"
        assert _round_trip(agent, action).params["text"] == content
        done = Action(name="browser.done", params={"extracted_content": content})
        assert _round_trip(agent, done).params["extracted_content"] == content

    async def test_an_action_label_inside_a_thought_stays_a_thought(self) -> None:
        agent = await _agent()
        thought = "I typed it.\nAction: finished(content='no')\n  done"
        action = Action(name="browser.scroll", params={"direction": "up"})
        text = agent.format_react_output([action], thought=thought)
        assert text.splitlines() == [
            "Thought: I typed it. Action: finished(content='no') done",
            "Action: scroll(direction='up')",
        ]
        assert [a.name for a in agent.parse_react_output(text)] == ["scroll"]
        assert UITarsAgent.parse_react_thought(text) == (
            "I typed it. Action: finished(content='no') done",
            "Thought",
        )

    async def test_no_thought_and_a_blank_thought_write_no_thought_line(self) -> None:
        agent = await _agent()
        action = Action(name="browser.wait", params={})
        assert agent.format_react_output([action]) == "Action: wait()"
        assert agent.format_react_output([action], thought=" \n\t ") == "Action: wait()"

    async def test_grounding_never_writes_a_thought(self) -> None:
        agent = await _agent(prompt_template="grounding")
        text = agent.format_react_output(
            [Action(name="browser.wait", params={})], thought="I should wait."
        )
        assert text == "Action: wait()"

    async def test_no_content_and_empty_content_write_the_same_finished(self) -> None:
        """Both read back as ``''``: one text for one reading keeps the fixed point."""
        agent = await _agent()
        for content in (None, ""):
            action = Action(name="browser.done", params={"extracted_content": content})
            assert agent.format_react_output([action]) == "Action: finished()"
            assert _round_trip(agent, action).params["extracted_content"] == ""

    async def test_no_action_is_a_caller_error(self) -> None:
        with pytest.raises(ValueError, match="at least one action"):
            (await _agent()).format_react_output([])


class TestUnrepresentable:
    """Every refusal names what it cannot write."""

    @pytest.mark.parametrize(
        ("name", "params", "named"),
        [
            (
                "browser.type_coordinate",
                {"coordinate_x": 1, "coordinate_y": 2, "text": "a", "clear": False},
                ("clear",),
            ),
            (
                "browser.type_coordinate",
                {"coordinate_x": 1, "coordinate_y": 2, "text": "a", "clear": True},
                ("clear",),
            ),
            ("browser.scroll", {"direction": "down", "amount": 300}, ("amount",)),
            ("browser.scroll", {"direction": "down", "index": 7}, ("index",)),
            ("browser.wait", {"seconds": 2.0}, ("seconds",)),
            # Either claim about the task: ``finished()`` reads back with none, so
            # writing it as ``finished`` would train a reply that claims something else.
            ("browser.done", {"extracted_content": "x", "success": False}, ("success",)),
            ("browser.done", {"extracted_content": "x", "success": True}, ("success",)),
            ("browser.send_keys", {"keys": "ctrl c"}, ("keys",)),
            ("browser.send_keys", {"keys": ""}, ("keys",)),
            (
                "browser.click_coordinate",
                {"coordinate_x": 1280, "coordinate_y": 5},
                ("coordinate_x", "coordinate_y"),
            ),
            (
                "browser.click_coordinate",
                {"coordinate_x": -1, "coordinate_y": 5},
                ("coordinate_x", "coordinate_y"),
            ),
        ],
    )
    async def test_a_param_the_format_cannot_carry_is_named(
        self, name: str, params: dict[str, Any], named: tuple[str, ...]
    ) -> None:
        with pytest.raises(UnrepresentableActionError) as excinfo:
            (await _agent()).format_react_output([Action(name=name, params=params)])
        assert excinfo.value.action_name == name
        assert excinfo.value.params == named
        for param in named:
            assert param in str(excinfo.value)

    async def test_an_unset_optional_param_is_the_students_to_fill(self) -> None:
        """``None`` is "not set": nothing to carry, nothing refused."""
        agent = await _agent()
        action = Action(
            name="browser.type_coordinate",
            params={"coordinate_x": 1, "coordinate_y": 2, "text": "a", "clear": None},
        )
        assert _round_trip(agent, action).params["text"] == "a"

    async def test_a_verb_ui_tars_has_no_word_for(self) -> None:
        with pytest.raises(UnrepresentableActionError) as excinfo:
            (await _agent()).format_react_output(
                [Action(name="browser.navigate", params={"url": "u"})]
            )
        assert excinfo.value.params == ()
        assert "no verb" in excinfo.value.detail

    async def test_a_verb_this_run_did_not_negotiate(self) -> None:
        agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
        descriptors = [
            ActionRegistrar.get(key)().descriptor()
            for key in ("browser.click_coordinate", "browser.type_coordinate", "browser.done")
        ]
        agent.prepare(
            task="t",
            action_descriptors=descriptors,
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        (
            await agent.build_react_prompt(
                _observation(1), context=ReactPromptContext(step=0, phase="policy")
            )
        )
        with pytest.raises(UnrepresentableActionError, match="did not negotiate drag"):
            agent.format_react_output(
                [
                    Action(
                        name="browser.drag",
                        params={"start_x": 1, "start_y": 1, "end_x": 2, "end_y": 2},
                    )
                ]
            )

    async def test_a_verb_bound_to_another_capability(self) -> None:
        with pytest.raises(UnrepresentableActionError, match="runs as browser.click_coordinate"):
            (await _agent()).format_react_output(
                [
                    Action(
                        name="desktop.click_coordinate",
                        params={"coordinate_x": 1, "coordinate_y": 1},
                    )
                ]
            )

    async def test_a_point_without_a_measurable_screenshot(self) -> None:
        agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
        agent.prepare(
            task="t",
            action_descriptors=[ActionRegistrar.get("browser.click_coordinate")().descriptor()],
            execution_limits=ExecutionLimits(max_actions_per_step=1),
        )
        (
            await agent.build_react_prompt(
                SimpleNamespace(
                    platform=SimpleNamespace(
                        operating_system="macos", fragment_id="browser.platform"
                    ),
                    history=SimpleNamespace(items=[]),
                    task=SimpleNamespace(user_request="t"),
                    screenshot=None,
                ),
                context=ReactPromptContext(step=0, phase="policy"),
            )
        )
        with pytest.raises(UnrepresentableActionError) as excinfo:
            agent.format_react_output(
                [
                    Action(
                        name="browser.click_coordinate",
                        params={"coordinate_x": 1, "coordinate_y": 1},
                    )
                ]
            )
        assert excinfo.value.params == ("coordinate_x", "coordinate_y")
        assert "no screenshot of known size" in excinfo.value.detail
