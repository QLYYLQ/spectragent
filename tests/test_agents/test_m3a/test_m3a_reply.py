"""M3A's reply: read as AndroidWorld reads it, and run as the framework's actions (``agents/m3a/reply.py``).

The reading is ``m3a_utils.parse_reason_action_output`` and the two
``extract_json``; the checks are ``JSONAction``'s; what each action type runs
as is AndroidWorld's ``actuation.py`` in the framework's actions, the claim and
the answer apart (``docs/architecture_design.md`` §4.1, "agent 的任务结论").
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.agents.m3a import M3AAgent
from spectragent.agents.m3a.reply import (
    NOT_AN_ACTION,
    NOT_IN_FORMAT,
    actions_of,
    read_reply,
)
from spectragent.core.loops.errors import ParseError
from spectragent.schemas.action import Action


def _actions(action: str) -> list[Action]:
    return M3AAgent().parse_react_output(f"Reason: because.\nAction: {action}")


class TestReading:
    def test_the_reason_and_the_action_written_back_as_json(self) -> None:
        reply = read_reply("Reason: Wi-Fi is on.\nAction: {'action_type': 'click', 'index': 3}")
        assert (reply.reason, reply.action) == (
            "Wi-Fi is on.",
            '{"action_type": "click", "index": 3}',
        )

    def test_the_reason_runs_to_the_last_action_label_and_the_action_from_the_first(self) -> None:
        """``Reason:(.*)Action:`` is greedy; ``Action:(.*)`` is not anchored to the last one."""
        reply = read_reply(
            'Reason: I said Action: wait before.\nAction: {"action_type": "wait"} then more'
        )
        assert reply.reason == "I said Action: wait before."
        assert reply.action == '{"action_type": "wait"}'

    def test_an_object_python_cannot_read_stays_as_written_and_is_read_as_json(self) -> None:
        """``true`` is JSON, not a Python literal: M3A keeps the text and reads it with ``json``."""
        reply = read_reply(
            'Reason: r\nAction: {"action_type": "input_text", "text": "a", "index": 1, '
            '"clear_text": true}'
        )
        assert reply.action == (
            '{"action_type": "input_text", "text": "a", "index": 1, "clear_text": true}'
        )
        assert actions_of(reply)[0].params["clear"] is True

    def test_text_beyond_ascii_is_escaped_as_json_dumps_writes_it(self) -> None:
        reply = read_reply("Reason: r\nAction: {'action_type': 'answer', 'text': '蓝牙'}")
        assert reply.action == '{"action_type": "answer", "text": "\\u84dd\\u7259"}'
        assert actions_of(reply) == [Action(name="done", params={"extracted_content": "蓝牙"})]

    @pytest.mark.parametrize(
        "text",
        [
            "Action: {'action_type': 'wait'}",
            "Reason: no action label here",
            "Reason:   \nAction: {'action_type': 'wait'}",
            "Reason: r\nAction:   ",
            "",
        ],
    )
    def test_a_reply_without_a_reason_or_an_action_is_not_in_the_format(self, text: str) -> None:
        with pytest.raises(ParseError) as refused:
            M3AAgent().parse_react_output(text)
        assert refused.value.detail == NOT_IN_FORMAT

    def test_the_reason_is_the_thought_the_training_export_reads(self) -> None:
        reply = "Reason: The toggle is on.\nAction: {'action_type': 'click', 'index': 2}"
        assert M3AAgent.parse_react_thought(reply) == ("The toggle is on.", "Reason")
        assert M3AAgent.parse_react_thought("Action: {}") is None


class TestWhatEachActionRunsAs:
    @pytest.mark.parametrize(
        ("action", "runs_as"),
        [
            ("{'action_type': 'click', 'index': 4}", [Action(name="click", params={"index": 4})]),
            (
                "{'action_type': 'long_press', 'index': '4'}",
                [Action(name="long_press", params={"index": 4})],
            ),
            (
                "{'action_type': 'keyboard_enter'}",
                [Action(name="press_key", params={"key": "enter"})],
            ),
            (
                "{'action_type': 'navigate_home'}",
                [Action(name="press_key", params={"key": "home"})],
            ),
            (
                "{'action_type': 'navigate_back'}",
                [Action(name="press_key", params={"key": "back"})],
            ),
            (
                "{'action_type': 'open_app', 'app_name': 'Clock'}",
                [Action(name="open_app", params={"app_name": "Clock"})],
            ),
            ("{'action_type': 'wait'}", [Action(name="wait", params={})]),
        ],
    )
    def test_an_action_type_runs_as_its_action(self, action: str, runs_as: list[Action]) -> None:
        assert _actions(action) == runs_as

    def test_input_text_types_into_the_element_without_clearing_then_presses_enter(self) -> None:
        """AndroidWorld's ``input_text`` taps the field, types and presses Enter; it clears only
        when ``clear_text`` says so, which M3A's prompt never teaches."""
        assert _actions("{'action_type': 'input_text', 'text': 'hello', 'index': 7}") == [
            Action(name="type", params={"index": 7, "text": "hello", "clear": False}),
            Action(name="press_key", params={"key": "enter"}),
        ]

    @pytest.mark.parametrize(
        "action",
        [
            "{'action_type': 'input_text', 'text': 'hello', 'clear_text': True}",
            "{'action_type': 'input_text', 'text': 'hello', 'clear_text': True, 'x': 10}",
        ],
    )
    def test_input_text_without_an_element_types_into_the_field_with_focus(
        self, action: str
    ) -> None:
        """Without an index or a whole point AndroidWorld taps nothing and types where focus is."""
        assert _actions(action) == [
            Action(name="type_coordinate", params={"text": "hello", "clear": True}),
            Action(name="press_key", params={"key": "enter"}),
        ]

    @pytest.mark.parametrize("direction", ["up", "down", "left", "right"])
    def test_a_scroll_keeps_its_direction_which_means_what_the_capabilitys_does(
        self, direction: str
    ) -> None:
        """M3A's direction is the content's: ``down`` shows what is further down. AndroidWorld
        swipes from the centre to the top for it, and so does Android's ``scroll`` for ``down``
        (``test_android_actions.py::test_a_scroll_means_what_it_means_on_the_browser``), so the
        direction passes through unchanged."""
        assert _actions(f"{{'action_type': 'scroll', 'direction': '{direction}'}}") == [
            Action(name="scroll", params={"direction": direction})
        ]

    def test_a_scroll_inside_an_element_names_its_index_element_zero_included(self) -> None:
        assert _actions("{'action_type': 'scroll', 'direction': 'down', 'index': 0}") == [
            Action(name="scroll", params={"direction": "down", "index": 0})
        ]

    def test_answer_ends_the_run_with_the_answer_and_no_claim(self) -> None:
        assert _actions("{'action_type': 'answer', 'text': 'three'}") == [
            Action(name="done", params={"extracted_content": "three"})
        ]

    @pytest.mark.parametrize(
        ("status", "params"),
        [
            ("complete", {"extracted_content": None, "success": True}),
            ("infeasible", {"extracted_content": None, "success": False}),
            ("finished", {"extracted_content": None}),
        ],
    )
    def test_status_ends_the_run_with_its_claim_and_no_answer(
        self, status: str, params: dict[str, Any]
    ) -> None:
        """AndroidWorld ends the run on any status; only these two say what the agent claims."""
        assert _actions(f"{{'action_type': 'status', 'goal_status': '{status}'}}") == [
            Action(name="done", params=params)
        ]

    @pytest.mark.parametrize(
        "action",
        [
            "{'action_type': 'tap', 'index': 1}",  # not an action type
            "{'action_type': 'double_tap', 'index': 1}",  # one M3A's prompt does not teach
            "{'action_type': 'swipe', 'direction': 'up'}",
            "{'action_type': 'click'}",  # no element
            "{'action_type': 'click', 'x': 10, 'y': 20}",  # a point, which this port does not run
            "{'action_type': 'click', 'index': 1, 'x': 10}",  # JSONAction: index or a point
            "{'action_type': 'click', 'index': 'first'}",
            "{'action_type': 'click', 'index': 1e999}",  # int() of it overflows
            '{"action_type": "click", "index": Infinity}',  # read as JSON
            "{'action_type': 'click', 'index': 1, 'target': 'ok'}",  # not a JSONAction field
            "{'action_type': 'input_text', 'index': 1}",  # no text: AndroidWorld types nothing
            "{'action_type': 'input_text', 'text': 'hi', 'x': 10, 'y': 20}",  # a point
            "{'action_type': 'scroll'}",  # no direction: AndroidWorld scrolls nothing
            "{'action_type': 'scroll', 'direction': 'Down'}",
            "{'action_type': 'open_app'}",
            "{'action_type': 'wait', 'keycode': 'ENTER'}",
            "['click', 1]",
            "no object at all",
        ],
    )
    def test_an_action_m3a_cannot_run_as_written_is_refused_in_its_words(self, action: str) -> None:
        with pytest.raises(ParseError) as refused:
            _actions(action)
        assert refused.value.detail == NOT_AN_ACTION
