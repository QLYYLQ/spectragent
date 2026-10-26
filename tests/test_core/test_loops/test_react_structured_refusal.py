"""A structured reply the provider refuses is refused to the model in the framework's words.

The provider validates a structured reply against the agent's per-run output
model and raises ``StructuredOutputValidationError`` with the text it saw.
ReAct words the refusal from that text, the output model and the run's action
space (``core/action_guard.py::reply_refusal``), in the voice of its params
check, and never hands the refused reply to the agent's parser: that parser
reads only a validated reply. The text is the same whether the model is asked
again within the step or on the next one.

Driven end to end: a real OpenAI provider against a scripted endpoint, a real
BrowserUse agent, ``ReactLoop``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from spectragent.agents.browser_use import BrowserUseDomAgent
from spectragent.core.action_guard import build_guarded_act, reply_refusal
from spectragent.core.capabilities.browser.observations import BrowserPlatformObs
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.loops.errors import ParseError
from spectragent.core.loops.react import ReactLoop, parse_feedback
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.action_spaces import browser_descriptors, space_of
from tests._helpers.fragment_formats import dom_formats
from tests._helpers.scripted_llm import ScriptedLLMServer

#: A reply with two mistakes: a reasoning field of the wrong type, and an
#: action missing a required parameter.
BAD = json.dumps(
    {"memory": 3, "action": [{"browser.click": {"index": 1}}, {"browser.type": {"index": 2}}]}
)
GOOD = json.dumps({"memory": "done", "action": [{"browser.done": {"extracted_content": "42"}}]})


class _Memory:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, message: Any) -> None:
        self.messages.append(message)

    def add_messages(self, messages: Any) -> None:
        self.messages.extend(messages)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


class _Env:
    """Just enough page for a BrowserUse prompt."""

    async def observe(self, _negotiated: Any) -> Any:
        return SimpleNamespace(
            task=SimpleNamespace(user_request="task", follow_ups=[]),
            platform=BrowserPlatformObs(operating_system="macos"),
            history=None,
            browser_meta=None,
            dom=None,
        )


class _Negotiated:
    loop_sourced = frozenset()


class _Run:
    """One run's outcome, and every reply its agent's parser was handed."""

    def __init__(self) -> None:
        self.result: Any = None
        self.memory = _Memory()
        self.recorder = StepRecorder()
        self.parsed: list[Any] = []
        self.model: Any = None
        self.space: Any = None

    def feedback(self) -> list[str]:
        return [
            str(message.content)
            for message in self.memory.messages
            if isinstance(message, UserMessage) and "could not be parsed" in str(message.content)
        ]


async def _run(replies: list[str], *, retries: int) -> _Run:
    out = _Run()

    async def execute(action: Action, **_kw: Any) -> ActionResult:
        done = action.name == "browser.done"
        return ActionResult(success=True, is_done=done, extracted_content="42" if done else None)

    descriptors = browser_descriptors("click", "type", "done")
    out.space, act_fn = build_guarded_act(
        execute, descriptors, bindings=space_of(descriptors).action_bindings
    )
    agent = BrowserUseDomAgent(flash_mode=True)
    agent.prepare(
        task="task",
        action_space=out.space,
        execution_limits=ReactLoop().execution_limits(),
        fragment_formats=dom_formats(),
    )
    out.model = agent.get_react_output_model()
    parse = agent.parse_react_output

    def spy(reply: Any) -> list[Action]:
        out.parsed.append(reply)
        return parse(reply)

    agent.parse_react_output = spy  # type: ignore[method-assign]
    loop = ReactLoop(max_steps=3, parse_retries_per_step=retries, max_consecutive_parse_errors=2)
    with ScriptedLLMServer(replies) as server:
        llm = ChatOpenAI(model=server.model, api_key="scripted", base_url=server.base_url)
        try:
            out.result = await loop._execute(
                agent,
                "task",
                ctx={
                    "env": _Env(),
                    "llm": llm,
                    "memory": out.memory,
                    "act_fn": act_fn,
                    "action_space": out.space,
                    "negotiated_obs": _Negotiated(),
                    "step_recorder": out.recorder,
                    "task_id": "t",
                },
            )
        finally:
            await llm.aclose()
    return out


async def test_the_refusal_is_the_framework_s_and_the_parser_never_sees_the_reply() -> None:
    run = await _run([BAD, GOOD], retries=1)
    assert run.result.agent_done == "done"

    (said,) = run.feedback()
    refusal = reply_refusal(run.space, run.model, BAD)
    assert refusal is not None
    assert said == parse_feedback(ParseError(refusal))
    # Every mistake located; the action as ReAct's params check words it.
    assert refusal.startswith(
        "the reply does not fit the output schema: memory: Input should be a valid string; "
        "action 2, browser.type: text: Field required (browser.type takes index: "
    )
    # Not the provider's pydantic dump.
    assert "validation error" not in said and "pydantic" not in said and "input_value" not in said
    # The parser read the one reply that fit, as the provider validated it.
    assert len(run.parsed) == 1 and isinstance(run.parsed[0], run.model)


async def test_the_in_step_and_the_next_step_refusal_are_one_text() -> None:
    in_step = await _run([BAD, GOOD], retries=1)
    next_step = await _run([BAD, GOOD], retries=0)
    assert next_step.result.agent_done == "done"
    assert in_step.feedback() == next_step.feedback()
    # Spent retries: the step is the parse failure, with the same refusal.
    first = next_step.recorder.records[0]
    assert first.error_phase == "parse" and first.actions == []
    assert first.error == reply_refusal(next_step.space, next_step.model, BAD)
    assert first.llm_output == BAD
