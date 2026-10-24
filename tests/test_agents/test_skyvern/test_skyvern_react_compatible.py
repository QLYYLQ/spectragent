"""The Skyvern family's ReactLoop slots: prompt, closed reply, action history, screenshot.

Every member shares the root's slots; the members differ only in what reaches
the model (``skyvern.dom_vision`` also shows the screenshot) and in the
actions they ask for. So each behaviour is checked on both members, and the
screenshot on each.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.rendering.prompt_context import restored_history_message
from spectragent.agents.skyvern import SkyvernAgent, SkyvernDomAgent, SkyvernDomVisionAgent
from spectragent.core.capabilities.browser.dom.models import DOMState
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.llm.messages import (
    BaseMessage,
    ContentPartImageParam,
    SystemMessage,
    UserMessage,
)
from spectragent.core.loops import ReactLoop
from spectragent.core.loops.coordinate_frames import declared_space, step_frame
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.fragments import HistoryObs
from spectragent.schemas.prompt import ReactPromptContext, RestoredHistory
from spectragent.schemas.restoration import RestoredAction
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_spaces import browser_space, wired_space
from tests._helpers.assembly import make_agent
from tests._helpers.fragment_formats import dom_formats

MEMBERS = [SkyvernDomAgent, SkyvernDomVisionAgent]
member_param = pytest.mark.parametrize("member", MEMBERS, ids=lambda cls: cls.__registry_key__)

#: The smallest PNG header ``image_media_type`` recognises.
_PNG = b"\x89PNG\r\n\x1a\n"


@dataclass
class _FakeBrowserMeta:
    url: str = ""
    page_title: str = ""
    tabs: list | None = None


@dataclass
class _FakeDOM:
    element_tree: Any = None


@dataclass
class _FakeTask:
    user_request: str = "find the login button"


@dataclass
class _FakeStepInfo:
    step: int = 1
    max_steps: int = 50
    today: str = "2026-05-01"


def _screenshot() -> ScreenshotObs:
    return ScreenshotObs(
        image_bytes=_PNG,
        image_width=2000,
        image_height=1000,
        viewport_width=1000,
        viewport_height=500,
    )


class _FakeObservation:
    def __init__(
        self,
        *,
        dom: _FakeDOM | None = None,
        browser_meta: _FakeBrowserMeta | None = None,
        task: _FakeTask | None = None,
        step_info: _FakeStepInfo | None = None,
        history: list[StepRecord] | None = None,
        screenshot: ScreenshotObs | None = None,
    ) -> None:
        self.dom = dom
        self.browser_meta = browser_meta
        self.task = task or _FakeTask()
        self.platform = SimpleNamespace(operating_system="macos", fragment_id="browser.platform")
        self.step_info = step_info
        self.history = HistoryObs(items=list(history or []))
        self.screenshot = screenshot


def _clicks(*indexes: int) -> list[StepRecord]:
    """One executed ``browser.click`` step per index, each with an ok result."""
    return [
        StepRecord(
            step=step,
            actions=[Action(name="browser.click", params={"index": index})],
            action_results=[ActionResult(success=True)],
        )
        for step, index in enumerate(indexes)
    ]


def _space(member: type[SkyvernAgent], agent: SkyvernAgent) -> ActionSpace:
    return wired_space(member, coordinate_space=declared_space(agent))


def _make_agent(member: type[SkyvernAgent], **overrides: Any) -> SkyvernAgent:
    """A member prepared with every action its wiring names, as a browser run gives it."""
    agent = make_agent(member, memory=SimpleMemory(max_messages=100), **overrides)
    agent.prepare(
        task="t",
        action_space=_space(member, agent),
        execution_limits=ExecutionLimits(max_actions_per_step=5),
        fragment_formats=dom_formats(),
    )
    return agent


def _system(member: type[SkyvernAgent], **overrides: Any) -> str:
    """The system prompt a member configured with *overrides* is seeded with."""
    agent = make_agent(member, **overrides)
    (seed,) = agent.prepare(
        task="t",
        action_space=_space(member, agent),
        execution_limits=ExecutionLimits(max_actions_per_step=5),
        fragment_formats=dom_formats(),
    )
    return seed.text


def _context(agent: SkyvernAgent, observation: Any, step: int = 0) -> ReactPromptContext:
    """The context a Loop hands *agent* for a step on *observation*, its frame included."""
    frame = step_frame(agent._action_space, observation)
    return ReactPromptContext(step=step, phase="policy", frame=frame)


async def _text(agent: SkyvernAgent, observation: Any, step: int = 0) -> str:
    (message,) = await agent.build_react_prompt(
        observation, context=_context(agent, observation, step)
    )
    return message.text


def _history_lines(text: str) -> list[str]:
    return text.split("<action_history>\n")[1].split("\n</action_history>")[0].splitlines()


def _reply(*actions: dict[str, Any]) -> str:
    return json.dumps({"actions": [{"reasoning": "r", "action": a} for a in actions]})


class TestSlotConformance:
    @member_param
    def test_has_required_react_slots(self, member: type[SkyvernAgent]) -> None:
        ReactLoop().validate_slots(_make_agent(member))

    @member_param
    def test_agent_id_is_the_member_key(self, member: type[SkyvernAgent]) -> None:
        agent = _make_agent(member)
        assert agent.agent_id == member.__registry_key__
        assert agent.agent_name == member.__registry_key__
        assert agent.domain == "web_navigation"


class TestPrepare:
    @member_param
    def test_returns_jinja_rendered_system_prompt_seed(self, member: type[SkyvernAgent]) -> None:
        agent = make_agent(member, memory=SimpleMemory(max_messages=100))
        seed = agent.prepare(
            task="Find the cheapest flight",
            action_space=_space(member, agent),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        assert len(seed) == 1
        assert isinstance(seed[0], SystemMessage)
        assert "Find the cheapest flight" in seed[0].content
        # Pure builder — agent memory remains untouched.
        assert agent.memory.get_messages() == []

    @member_param
    def test_renders_the_negotiated_action_set(self, member: type[SkyvernAgent]) -> None:
        agent = make_agent(member)
        space = _space(member, agent)
        text = agent.prepare(
            task="t",
            action_space=space,
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )[0].text
        for descriptor in space.descriptors:
            assert f"`{descriptor.name}(" in text
            assert descriptor.description in text

    @member_param
    def test_renders_the_loops_action_budget(self, member: type[SkyvernAgent]) -> None:
        agent = make_agent(member)
        text = agent.prepare(
            task="t",
            action_space=_space(member, agent),
            execution_limits=ExecutionLimits(max_actions_per_step=4),
            fragment_formats=dom_formats(),
        )[0].text
        assert {int(n) for n in re.findall(r"maximum of (\d+) actions", text)} == {4}

    @member_param
    def test_quotes_the_dom_serializers_own_description(self, member: type[SkyvernAgent]) -> None:
        """The element tree's format is the serializer's words, not the template's."""
        from spectragent.core.capabilities.browser.dom.serializers.html_serializer import (
            HTMLSerializer,
        )

        agent = make_agent(member)
        formats = dom_formats(HTMLSerializer())
        text = agent.prepare(
            task="t",
            action_space=_space(member, agent),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=formats,
        )[0].text
        assert formats["dom"] in text
        assert 'data-idx="' in formats["dom"]

    @member_param
    def test_states_how_actions_run(self, member: type[SkyvernAgent]) -> None:
        """What the prompt says about execution is what the Loop and the capability do.

        The Loop stops a batch at its first failed action (the user's ruling
        of 2026-10-24) and at a ``done`` or an action that raises; a page
        change does not stop it. Indices are backend-node handles, stable
        across observations.
        """
        text = _system(member)
        assert "re-assigned every step" not in text
        assert "even when the one before it failed" not in text
        assert "An action\nthat fails stops the step: the actions after it are not executed" in text
        assert "An action that changes the page does not stop it" in text
        assert "carries `not_executed`" in text
        assert "indices are not renumbered between steps" in text

    @member_param
    def test_optional_config_blocks_render_only_when_set(self, member: type[SkyvernAgent]) -> None:
        off = _system(member)
        assert "PAY ONLY WITH THE SAVED CARD" not in off
        assert "GIVE UP AFTER A PAYWALL" not in off
        assert "verification code" not in off

        on = _system(
            member,
            complete_criterion="PAY ONLY WITH THE SAVED CARD",
            terminate_criterion="GIVE UP AFTER A PAYWALL",
            verification_code_check=True,
            navigation_payload="username=bob",
        )
        assert "PAY ONLY WITH THE SAVED CARD" in on
        assert "GIVE UP AFTER A PAYWALL" in on
        assert "verification code" in on
        assert "username=bob" in on

    @member_param
    def test_refuses_a_run_with_no_action(self, member: type[SkyvernAgent]) -> None:
        with pytest.raises(ValueError, match="at least one negotiated action"):
            make_agent(member).prepare(
                task="t",
                action_space=ActionSpace(),
                execution_limits=ExecutionLimits(max_actions_per_step=1),
                fragment_formats=dom_formats(),
            )


class TestBuildReactPrompt:
    @member_param
    async def test_includes_dom_text(self, member: type[SkyvernAgent]) -> None:
        """A real ``DOMState``: the model reads its ``serialized_dom``, never the repr."""
        agent = _make_agent(member)
        state = DOMState(serialized_dom="[1]<button>Submit</button>\n[2]<input placeholder='q'/>")
        content = await _text(
            agent,
            _FakeObservation(
                dom=_FakeDOM(element_tree=state),
                browser_meta=_FakeBrowserMeta(url="https://www.example.com", page_title="Ex"),
                task=_FakeTask(user_request="find the cheapest flight"),
                screenshot=_screenshot(),
            ),
        )
        assert "https://www.example.com" in content
        assert "[1]<button>Submit</button>" in content
        assert "DOMElementNode(" not in content
        assert "serialized_dom=" not in content

    @member_param
    async def test_dom_text_is_not_cut_by_the_agent(self, member: type[SkyvernAgent]) -> None:
        agent = _make_agent(member)
        tail = "[173]<button>Reachable at the end</button>"
        state = DOMState(serialized_dom="x" * 10_100 + tail)
        content = await _text(agent, _FakeObservation(dom=_FakeDOM(element_tree=state)))
        assert tail in content

    @member_param
    async def test_action_history_is_rendered_per_step_not_in_the_system_prompt(
        self, member: type[SkyvernAgent]
    ) -> None:
        agent = _make_agent(member, action_history_window=2)
        assert "<action_history>" not in agent._system_prompt_text

        first = await _text(agent, _FakeObservation(dom=_FakeDOM(element_tree="<x/>")))
        assert "<action_history>\n(no actions yet)\n</action_history>" in first

        later = await _text(
            agent,
            _FakeObservation(dom=_FakeDOM(element_tree="<x/>"), history=_clicks(0, 1, 2)),
            step=3,
        )
        lines = _history_lines(later)
        # window=2 → the two most recent actions, oldest dropped.
        assert [json.loads(line)["action"]["params"] for line in lines] == [
            {"index": 1},
            {"index": 2},
        ]

    @member_param
    async def test_a_zero_window_renders_nothing_not_everything(
        self, member: type[SkyvernAgent]
    ) -> None:
        """``lst[-0:]`` is ``lst[0:]``."""
        agent = _make_agent(member, action_history_window=0)
        content = await _text(
            agent, _FakeObservation(dom=_FakeDOM(element_tree="<x/>"), history=_clicks(0, 1, 2))
        )
        assert "<action_history>\n(disabled)\n</action_history>" in content
        assert "browser.click" not in content

    @member_param
    async def test_user_request_and_step_info(self, member: type[SkyvernAgent]) -> None:
        content = await _text(
            _make_agent(member),
            _FakeObservation(
                task=_FakeTask(user_request="reset the user password"),
                step_info=_FakeStepInfo(step=2, max_steps=10, today="2026-05-01"),
            ),
        )
        assert "<user_request>\nreset the user password\n</user_request>" in content
        assert "<step_info>step 2/10 (today=2026-05-01)</step_info>" in content

    @member_param
    async def test_returns_only_the_new_user_message(self, member: type[SkyvernAgent]) -> None:
        agent = _make_agent(member)
        observation = _FakeObservation(dom=_FakeDOM(element_tree="<x/>"), screenshot=_screenshot())
        messages = await agent.build_react_prompt(observation, context=_context(agent, observation))
        assert len(messages) == 1
        assert isinstance(messages[0], UserMessage)
        assert all(isinstance(m, BaseMessage) for m in messages)

    @member_param
    async def test_a_parsed_reply_never_enters_the_window(self, member: type[SkyvernAgent]) -> None:
        """The window is the Loop's record of what ran, not what was proposed."""
        agent = _make_agent(member)
        agent.parse_react_output(_reply({"browser.click": {"index": 7}}))
        content = await _text(agent, _FakeObservation())
        assert "<action_history>\n(no actions yet)\n</action_history>" in content


class TestActionHistory:
    """Each line is ``{"action", "result"}`` as ``result_facts`` writes it, returned content included."""

    @member_param
    async def test_an_executed_action_enters_the_window_with_its_result(
        self, member: type[SkyvernAgent]
    ) -> None:
        typed = StepRecord(
            step=0,
            actions=[Action(name="browser.type", params={"index": 4, "text": "hello"})],
            action_results=[ActionResult(success=True)],
        )
        (line,) = _history_lines(
            await _text(_make_agent(member), _FakeObservation(history=[typed]), step=1)
        )
        assert json.loads(line) == {
            "action": {"name": "browser.type", "params": {"index": 4, "text": "hello"}},
            "result": {
                "success": True,
                "error": None,
                "is_done": False,
                "extracted_content": None,
            },
        }

    @member_param
    async def test_extracted_content_reaches_the_model(self, member: type[SkyvernAgent]) -> None:
        """The defect the end-to-end run found: the extraction was run and never shown.

        The old line was ``name(params) -> ok``, so a model that extracted the
        page's K-line table read only "ok" and finished claiming it had the
        data.
        """
        extracted = StepRecord(
            step=0,
            actions=[Action(name="browser.extract_content", params={"index": 673})],
            action_results=[
                ActionResult(success=True, extracted_content="本周最高 3,412.08 最低 3,301.55")
            ],
        )
        (line,) = _history_lines(
            await _text(_make_agent(member), _FakeObservation(history=[extracted]), step=1)
        )
        assert "本周最高 3,412.08 最低 3,301.55" in line
        assert json.loads(line)["result"]["extracted_content"] == "本周最高 3,412.08 最低 3,301.55"

    @member_param
    async def test_returned_content_is_bounded_and_says_so(
        self, member: type[SkyvernAgent]
    ) -> None:
        page_text = "a" * 30 + "b" * 70
        extracted = StepRecord(
            step=0,
            actions=[Action(name="browser.extract_content", params={})],
            action_results=[ActionResult(success=True, extracted_content=page_text)],
        )
        agent = _make_agent(member, max_result_chars=30)
        (line,) = _history_lines(await _text(agent, _FakeObservation(history=[extracted]), step=1))
        shown = json.loads(line)["result"]["extracted_content"]
        assert shown == "a" * 30 + " ... [30 of 100 characters shown]"
        assert re.search(r"cut to 30\s+characters", agent._system_prompt_text)

    @member_param
    async def test_live_and_restored_history_show_the_same_result_facts(
        self, member: type[SkyvernAgent]
    ) -> None:
        """A live step and a restored one read alike: one ``result_facts``, one bound."""
        agent = _make_agent(member, max_result_chars=8)
        action = Action(name="browser.extract_content", params={"index": 3})
        result = ActionResult(success=True, extracted_content="0123456789abcdef")
        (line,) = _history_lines(
            await _text(
                agent,
                _FakeObservation(
                    history=[StepRecord(step=0, actions=[action], action_results=[result])]
                ),
                step=1,
            )
        )
        restored = restored_history_message(
            RestoredHistory(
                source_run_id="source",
                source_step=0,
                source_reply="{}",
                source_actions=[action],
                actions=[action],
                results=[result],
                executions=[
                    RestoredAction(
                        recorded=action,
                        executed=action,
                        method="identity",
                        recorded_success=True,
                        live_success=True,
                        result=result,
                    )
                ],
            ),
            show_action_effects=agent.show_action_effects,
            context=ReactPromptContext(step=0, phase="history"),
            action_space=agent._action_space,
            max_content_chars=agent.max_result_chars,
        )
        facts = json.loads(restored.text.split("\n", 1)[1])
        (restored_entry,) = facts["actual_restored_execution"]
        assert restored_entry == json.loads(line)

    @member_param
    async def test_an_action_that_raised_says_so(self, member: type[SkyvernAgent]) -> None:
        record = StepRecord(
            step=0,
            actions=[
                Action(name="browser.click", params={"index": 1}),
                Action(name="browser.click", params={"index": 2}),
            ],
            action_results=[ActionResult(success=True)],
            error_phase="act",
            error="CDP connection lost",
        )
        lines = _history_lines(await _text(_make_agent(member), _FakeObservation(history=[record])))
        assert [json.loads(line).get("raised") for line in lines] == [None, "CDP connection lost"]


class TestParseReactOutput:
    @member_param
    def test_parses_the_closed_reply(self, member: type[SkyvernAgent]) -> None:
        agent = _make_agent(member)
        actions = agent.parse_react_output(
            _reply(
                {"browser.click": {"index": 7}},
                {"browser.scroll": {"direction": "up", "amount": None, "index": None}},
                {"browser.type": {"index": 3, "text": "hello", "clear": None}},
            )
        )
        # The reasoning stays in the reply (``parse_react_thought`` reads it);
        # a declined optional param is left out, so the capability's knob decides.
        assert actions == [
            Action(name="browser.click", params={"index": 7}),
            Action(name="browser.scroll", params={"direction": "up"}),
            Action(name="browser.type", params={"index": 3, "text": "hello"}),
        ]

    @member_param
    def test_a_param_the_action_does_not_take_goes_back_to_the_model(
        self, member: type[SkyvernAgent]
    ) -> None:
        """The flat item let a click carry ``text``; the params check then refused the whole reply."""
        with pytest.raises(ParseError, match="browser.click: text"):
            _make_agent(member).parse_react_output(
                _reply({"browser.click": {"index": 7, "text": "Search"}})
            )

    @member_param
    def test_an_unknown_or_bare_verb_goes_back_to_the_model(
        self, member: type[SkyvernAgent]
    ) -> None:
        """The schema permits only the run's fully-qualified names."""
        agent = _make_agent(member)
        for verb in ("browser.CLICK", "click", "browser.navigate"):
            with pytest.raises(ParseError, match="unknown action"):
                agent.parse_react_output(_reply({verb: {"index": 1}}))

    @member_param
    def test_done_keeps_its_required_null_answer(self, member: type[SkyvernAgent]) -> None:
        """``extracted_content`` is required and nullable; a written null is the answer."""
        (action,) = _make_agent(member).parse_react_output(
            _reply({"browser.done": {"extracted_content": None, "success": None}})
        )
        assert action == Action(name="browser.done", params={"extracted_content": None})

    @member_param
    def test_done_carries_the_claim(self, member: type[SkyvernAgent]) -> None:
        (action,) = _make_agent(member).parse_react_output(
            _reply({"browser.done": {"extracted_content": "hit a paywall", "success": False}})
        )
        assert action.params == {"extracted_content": "hit a paywall", "success": False}

    @member_param
    def test_an_empty_action_list_goes_back_to_the_model(self, member: type[SkyvernAgent]) -> None:
        with pytest.raises(ParseError):
            _make_agent(member).parse_react_output('{"actions": []}')

    @member_param
    def test_text_that_is_not_json_goes_back_to_the_model(self, member: type[SkyvernAgent]) -> None:
        with pytest.raises(ParseError, match="not valid JSON"):
            _make_agent(member).parse_react_output("click 7")

    @member_param
    def test_before_prepare_there_is_nothing_to_validate_against(
        self, member: type[SkyvernAgent]
    ) -> None:
        with pytest.raises(ParseError, match="before prepare"):
            member().parse_react_output(_reply({"browser.click": {"index": 1}}))
        with pytest.raises(RuntimeError, match="before prepare"):
            member().get_react_output_model()


class TestOutputModel:
    @member_param
    def test_the_reply_survives_a_strict_provider_schema(self, member: type[SkyvernAgent]) -> None:
        """Closed all the way down, so OpenAI-style ``strict: true`` constrains it for real."""
        from spectragent.core.llm.schema.optimizer import SchemaOptimizer

        model = _make_agent(member).get_react_output_model()
        schema = SchemaOptimizer.create_optimized_json_schema(model, strict=True)
        assert SchemaOptimizer.is_strict_compatible(schema)
        item = schema["properties"]["actions"]["items"]
        assert item["additionalProperties"] is False
        assert set(item["required"]) == {"reasoning", "action"}

    @member_param
    def test_the_schema_holds_exactly_the_run_s_verbs(self, member: type[SkyvernAgent]) -> None:
        agent = _make_agent(member)
        schema = json.dumps(agent.get_react_output_model().model_json_schema())
        for descriptor in agent._action_space.descriptors:
            assert f'"{descriptor.name}"' in schema
        assert '"browser.navigate"' not in schema


class TestScreenshot:
    """Captured in both members; shown to the model only by ``skyvern.dom_vision``."""

    async def test_dom_never_shows_the_screenshot(self) -> None:
        agent = _make_agent(SkyvernDomAgent)
        observation = _FakeObservation(screenshot=_screenshot())
        (message,) = await agent.build_react_prompt(
            observation, context=_context(agent, observation)
        )
        assert isinstance(message.content, str)
        assert "Screenshot" not in message.content
        assert "screenshot" not in agent._system_prompt_text.lower()

    async def test_dom_vision_shows_the_screenshot_with_its_size(self) -> None:
        agent = _make_agent(SkyvernDomVisionAgent)
        observation = _FakeObservation(screenshot=_screenshot())
        (message,) = await agent.build_react_prompt(
            observation, context=_context(agent, observation)
        )
        assert isinstance(message.content, list)
        images = [part for part in message.content if isinstance(part, ContentPartImageParam)]
        assert len(images) == 1
        assert images[0].image_url.detail == "auto"
        assert images[0].image_url.url.startswith("data:image/png;base64,")
        assert "Screenshot: 2000x1000 pixels" in message.text
        assert "<screenshot>" in agent._system_prompt_text

    async def test_dom_vision_says_when_there_is_no_screenshot(self) -> None:
        agent = _make_agent(SkyvernDomVisionAgent)
        observation = _FakeObservation(screenshot=None)
        (message,) = await agent.build_react_prompt(
            observation, context=_context(agent, observation)
        )
        assert isinstance(message.content, str)
        assert "No screenshot could be shown this step." in message.content

    async def test_dom_vision_writes_history_points_on_the_step_s_grid(self) -> None:
        """A ``click_coordinate`` the run executed in CSS px reads back on the model's grid."""
        agent = _make_agent(SkyvernDomVisionAgent)
        observation = _FakeObservation(screenshot=_screenshot())
        frame = step_frame(agent._action_space, observation)
        clicked = StepRecord(
            step=0,
            frame=frame,
            actions=[
                Action(
                    name="browser.click_coordinate", params={"coordinate_x": 7, "coordinate_y": 14}
                )
            ],
            action_results=[ActionResult(success=True)],
        )
        (line,) = _history_lines(
            await _text(agent, _FakeObservation(history=[clicked], screenshot=_screenshot()))
        )
        # Image 2000x1000 over a 1000x500 viewport: pixel points are twice the CSS px.
        assert json.loads(line)["action"]["params"] == {"coordinate_x": 14, "coordinate_y": 28}

    def test_dom_vision_teaches_the_grid_only_with_a_point_action(self) -> None:
        with_point = _system(SkyvernDomVisionAgent)
        assert "in pixels of\nthis step's screenshot" in with_point

        agent = make_agent(SkyvernDomVisionAgent)
        agent.prepare(
            task="t",
            action_space=browser_space(
                "click", "type", "done", coordinate_space=agent.coordinate_space
            ),
            execution_limits=ExecutionLimits(max_actions_per_step=1),
            fragment_formats=dom_formats(),
        )
        without = agent._system_prompt_text
        assert "<screenshot>" in without
        assert "takes a point" not in without


class TestFrameElementIndices:
    """A ten-digit frame-element index survives the closed Skyvern shape."""

    @member_param
    async def test_the_index_reaches_the_element_unchanged(
        self, member: type[SkyvernAgent]
    ) -> None:
        from spectragent.core.capabilities.browser.actions.click import ClickAction
        from tests.test_core.test_capabilities.test_browser_actions.conftest import (
            FakeCapability,
            FakeElementNode,
            FakeScope,
        )

        key = 3 * 2**31 + 7
        (action,) = _make_agent(member).parse_react_output(
            _reply({"browser.click": {"index": key}})
        )
        assert action.params["index"] == key

        capability = FakeCapability()
        capability._scopes["tab"] = FakeScope()
        capability._selector_maps["tab"] = {
            key: FakeElementNode(backend_node_id=7, session_id="frame")
        }
        capability._default_session_id = "tab"
        result = await ClickAction(post_delay_ms=0).execute(action.params, capability=capability)  # type: ignore[arg-type]
        assert result.success, result.error
        (event,) = capability._scopes["tab"].emitted
        assert (event.backend_node_id, event.session_id) == (7, "frame")
