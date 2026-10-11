"""``parse_react_thought``: the teacher's thought, read off a recorded reply (arch §4.1, M32).

An optional ReAct slot the training export calls on the teacher's *class*
(the teacher is never composed), so it is a pure function of the reply:
a ``@staticmethod``. Each agent knows where its own replies keep their
reasoning, and says which field it took the thought from.
"""

from __future__ import annotations

import inspect
import json

import pytest

from spectragent.agents._output_models import REASONING_FIELDS, SkyvernActionItem, SkyvernOutput
from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent


@pytest.mark.parametrize("cls", [UITarsAgent, BrowserUseAgent, SkyvernAgent])
def test_it_is_a_pure_function_of_the_reply(cls: type) -> None:
    assert isinstance(inspect.getattr_static(cls, "parse_react_thought"), staticmethod)


class TestUITars:
    def test_the_native_thought_up_to_the_first_action_line(self) -> None:
        reply = (
            "Thought: The cart link is top right.\nI will click it.\nAction: click(point='(1,2)')"
        )
        assert UITarsAgent.parse_react_thought(reply) == (
            "The cart link is top right.\nI will click it.",
            "Thought",
        )

    def test_dressed_labels_and_a_second_action_line(self) -> None:
        reply = "**Thought:** go\n\n**Action:** wait()\nAction: wait()"
        assert UITarsAgent.parse_react_thought(reply) == ("go", "Thought")

    def test_no_thought_or_an_empty_one_is_none(self) -> None:
        assert UITarsAgent.parse_react_thought("Action: wait()") is None
        assert UITarsAgent.parse_react_thought("Thought:   \nAction: wait()") is None

    def test_thought_in_the_middle_of_prose_is_not_a_label(self) -> None:
        assert UITarsAgent.parse_react_thought("my thought: x\nAction: wait()") is None


def _browser_use_reply(fields: tuple[str, ...]) -> str:
    """A reply carrying every reasoning field *fields* names, each with its own text."""
    data: dict[str, object] = {"action": [{"wait": {}}]}
    for name in fields:
        if name == "current_plan_item":
            data[name] = 1
        elif name == "plan_update":
            data[name] = ["a step"]
        else:
            data[name] = f"  the {name} text  "
    return json.dumps(data)


class TestBrowserUse:
    @pytest.mark.parametrize("variant", sorted(REASONING_FIELDS))
    def test_every_template_variant(self, variant: str) -> None:
        """thinking > next_goal > memory, whichever the variant teaches first."""
        fields = REASONING_FIELDS[variant]
        expected = next(name for name in ("thinking", "next_goal", "memory") if name in fields)
        assert BrowserUseAgent.parse_react_thought(_browser_use_reply(fields)) == (
            f"the {expected} text",
            expected,
        )

    def test_flash_teaches_only_memory(self) -> None:
        assert REASONING_FIELDS["system_prompt_flash"] == ("memory",)
        reply = _browser_use_reply(REASONING_FIELDS["system_prompt_flash"])
        assert BrowserUseAgent.parse_react_thought(reply) == ("the memory text", "memory")

    def test_an_empty_field_falls_through_to_the_next(self) -> None:
        reply = json.dumps({"thinking": " ", "next_goal": "", "memory": "m", "action": []})
        assert BrowserUseAgent.parse_react_thought(reply) == ("m", "memory")

    def test_nothing_to_read_is_none(self) -> None:
        assert BrowserUseAgent.parse_react_thought(json.dumps({"action": []})) is None
        assert BrowserUseAgent.parse_react_thought("not json") is None
        assert BrowserUseAgent.parse_react_thought(json.dumps(["a", "list"])) is None

    def test_a_parsed_model_reads_like_its_json(self) -> None:
        from pydantic import create_model

        model = create_model("Reply", next_goal=(str, ""), memory=(str, ""))
        assert BrowserUseAgent.parse_react_thought(model(next_goal="go", memory="m")) == (
            "go",
            "next_goal",
        )


class TestSkyvern:
    def test_every_reasoning_in_order(self) -> None:
        reply = SkyvernOutput(
            actions=[
                SkyvernActionItem(action_type="browser.click", index=3, reasoning=" open it "),
                SkyvernActionItem(action_type="browser.type", index=4, text="x", reasoning=""),
                SkyvernActionItem(action_type="browser.scroll", direction="down", reasoning="more"),
            ]
        )
        expected = ("open it\nmore", "reasoning")
        assert SkyvernAgent.parse_react_thought(reply.model_dump_json()) == expected
        assert SkyvernAgent.parse_react_thought(reply) == expected

    def test_no_reasoning_is_none(self) -> None:
        reply = SkyvernOutput(actions=[SkyvernActionItem(action_type="browser.click", index=3)])
        assert SkyvernAgent.parse_react_thought(reply.model_dump_json()) is None
        assert SkyvernAgent.parse_react_thought("{") is None
        assert SkyvernAgent.parse_react_thought(json.dumps({"actions": "x"})) is None
