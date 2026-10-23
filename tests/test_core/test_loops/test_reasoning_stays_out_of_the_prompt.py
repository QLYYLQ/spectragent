"""The model's hidden reasoning is recorded, and never reaches the next prompt.

The user's ruling of 2026-10-14: the reasoning a provider returns is recorded in the
call record's ``thinking`` field, is never fed back to the model, and is not read by
the agent (``docs/architecture_design.md`` §4.7).

What carries that out is a narrow seam: the Loop puts only a completion's
``completion`` into memory, and an agent builds each turn from its observation and
that reply. So two runs that differ only in whether the model's replies carry
reasoning must send the model the very same requests, byte for byte, while the
reasoning lands in the trace. Both runs here go through the real OpenAI-compatible
provider, whose replies arrive over ``httpx.MockTransport`` with the reasoning in
``reasoning_content``, the field dmxapi.cn returns for ``qwen3.8-flash``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.action_guard import build_guarded_act
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    BrowserPlatformObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.loops.react import ReactLoop
from spectragent.core.negotiation import negotiate_action
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation.fragments import HistoryObs, StepInfoObs, TaskObs
from tests._helpers.action_params import minimal_params
from tests._helpers.assembly import AGENT_ID, make_env, make_run

_THINKING = "HIDDEN REASONING: the second field looks like the search box."
_PNG = b"\x89PNG\r\n\x1a\n-pretend-this-is-a-screenshot"


class _Endpoint:
    """An OpenAI-compatible endpoint that answers from a script and keeps every request."""

    def __init__(self, replies: list[str], *, reasoning: str | None) -> None:
        self.replies = replies
        self.reasoning = reasoning
        self.requests: list[httpx.Request] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        content = self.replies[min(len(self.requests), len(self.replies)) - 1]
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if self.reasoning is not None:
            message["reasoning_content"] = self.reasoning
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(self.requests)}",
                "object": "chat.completion",
                "created": 0,
                "model": "qwen3.8-flash",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    def llm(self) -> ChatOpenAI:
        return ChatOpenAI(
            model="qwen3.8-flash",
            api_key="stub",
            base_url="https://gateway.test/v1",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
        )

    @property
    def bodies(self) -> list[bytes]:
        return [request.content for request in self.requests]


class _Recording:
    """The provider, keeping every completion it handed the Loop."""

    inference_kind = "chat"

    def __init__(self, llm: ChatOpenAI) -> None:
        self._llm = llm
        self.model = llm.model
        self.completions: list[Any] = []

    async def ainvoke(self, messages: Any, output_format: Any = None, **kwargs: Any) -> Any:
        completion = await self._llm.ainvoke(messages, output_format, **kwargs)
        self.completions.append(completion)
        return completion


def _assert_never_sent(bodies: list[bytes]) -> None:
    for index, body in enumerate(bodies):
        assert _THINKING.encode() not in body, f"request {index} carried the model's reasoning"


# ── The production path: runtime, trace writer, memory, loop ──────────


class _Cap(CapabilityBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "reasoning_prompt_cap"
    __wiring__ = {
        "action": ("action", ["reasoning_prompt_cap.done", "reasoning_prompt_cap.click"]),
        "observation": ("observation", ["browser.browser_meta"]),
    }

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [
            ActionDescriptor(name="reasoning_prompt_cap.done", description="Done"),
            ActionDescriptor(name="reasoning_prompt_cap.click", description="Click"),
        ]

    async def execute_action(self, action: Action, **kw: Any) -> ActionResult:
        if action.name.endswith("done"):
            return ActionResult(success=True, is_done=True, extracted_content="42")
        return ActionResult(success=True, extracted_content="clicked")

    async def resolve_fragment(self, fragment_key: str, **kwargs: Any) -> Any:
        if fragment_key == "browser.browser_meta":
            return BrowserMetaObs(url="https://example.com/search", page_title="Search")
        return None


class _Env(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "reasoning_prompt_env"


class _Agent:
    """Builds each turn from its observation, and acts on the reply it is handed."""

    __wiring__ = {
        "action": ("action", ["done", "click"]),
        "observation": ("observation", ["browser.browser_meta", "loop.task"]),
    }

    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs: Any, *, context) -> list[UserMessage]:
        return [UserMessage(content=f"{obs.task.user_request} — now on {obs.browser_meta.url}")]

    def parse_react_output(self, out: Any) -> list[Action]:
        if out == "done":
            return [Action(name="done", params={})]
        return [Action(name="click", params={"index": 1})]

    def get_react_output_model(self) -> None:
        return None


async def _run_with_trace(tmp_path: Any, *, reasoning: str | None) -> tuple[_Endpoint, Any, Any]:
    endpoint = _Endpoint(["click", "done"], reasoning=reasoning)
    writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
    memory = SimpleMemory(max_messages=100)
    run = make_run(
        agent=_Agent(),
        loop=ReactLoop(max_steps=3),
        env=make_env(_Env, {"reasoning_prompt_cap": _Cap()}),
        llm=endpoint.llm(),
        memory=memory,
        hooks=[writer],
    )
    result = await AgentRuntime.for_agent(run, AGENT_ID).execute(
        "find the price", task_id="reasoning-1", timeout=10
    )
    assert result.agent_done == AgentDoneStatus.DONE
    assert writer.run_dir is not None
    return endpoint, RunTrace(writer.run_dir), memory


class TestTheProductionPath:
    async def test_the_reasoning_lands_in_the_call_record_and_reads_back(self, tmp_path) -> None:
        _endpoint, trace, _memory = await _run_with_trace(tmp_path, reasoning=_THINKING)

        for step in (0, 1):
            (call,) = trace.llm_messages(step)
            assert call.completion is not None
            assert call.completion.thinking == _THINKING
            assert _THINKING not in json.dumps(call.completion.completion)
        record = trace.manifest.steps[0].llm_calls[0]
        raw = json.loads((trace.trace_dir / record).read_text())
        assert raw["completion"]["thinking"] == _THINKING

    async def test_the_next_prompt_is_the_one_a_reply_without_reasoning_gets(
        self, tmp_path
    ) -> None:
        with_reasoning, _trace, memory = await _run_with_trace(
            tmp_path / "with", reasoning=_THINKING
        )
        without, _, _ = await _run_with_trace(tmp_path / "without", reasoning=None)

        assert len(with_reasoning.bodies) == 2
        # The second request is the turn after a reply that carried reasoning.
        assert with_reasoning.bodies == without.bodies
        _assert_never_sent(with_reasoning.bodies)
        assert all(_THINKING not in str(m.content) for m in memory.get_messages())


# ── Every shipped agent: the turn it builds after such a reply ────────


class _Env2:
    """Stands in for the browser: one canned observation, every action a no-op."""

    def __init__(self, observation: Any) -> None:
        self._observation = observation
        self.acted: list[str] = []

    async def observe(self, *args: Any, **kwargs: Any) -> Any:
        return self._observation

    async def act(self, action: Action, **kwargs: Any) -> ActionResult:
        self.acted.append(action.name)
        if action.name == "browser.done":
            return ActionResult(success=True, is_done=True, extracted_content="42")
        return ActionResult(success=True)


class _Negotiated:
    """The observation is a plain object, so no loop fragment is populated onto it."""

    active_fragments = {"dom", "screenshot", "browser_meta", "platform", "loop.task"}
    env_sourced = {"dom", "screenshot", "browser_meta", "platform"}
    loop_sourced: set[str] = set()


def _observation() -> Any:
    return type(
        "Obs",
        (),
        {
            "platform": BrowserPlatformObs(operating_system="macos"),
            "dom": DOMObs(),
            "screenshot": ScreenshotObs(
                image_bytes=_PNG,
                viewport_width=1280,
                viewport_height=720,
                image_width=1280,
                image_height=720,
            ),
            "browser_meta": BrowserMetaObs(url="https://example.com", page_title="Example"),
            "task": TaskObs(user_request="Find the answer"),
            "step_info": StepInfoObs(step=1, max_steps=3, today="2026-10-14"),
            "history": HistoryObs(),
        },
    )()


class _ActionsOnlyEnv:
    def __init__(self, capability: Any) -> None:
        self.provided_actions = capability.provided_actions


def _browser_use_replies(descriptors: list[ActionDescriptor]) -> list[str]:
    scroll = next(d for d in descriptors if d.name == "browser.scroll")
    return [
        json.dumps({"memory": "Scrolling.", "action": [{scroll.name: minimal_params(scroll)}]}),
        json.dumps(
            {"memory": "Found it.", "action": [{"browser.done": {"extracted_content": "42"}}]}
        ),
    ]


def _skyvern_replies(_descriptors: list[ActionDescriptor]) -> list[str]:
    return [
        json.dumps(
            {
                "actions": [
                    {"action_type": "browser.scroll", "direction": "down", "reasoning": "Below."}
                ],
                "is_done": False,
            }
        ),
        json.dumps({"actions": [], "is_done": True, "success": True, "extracted_content": "42"}),
    ]


def _ui_tars_replies(_descriptors: list[ActionDescriptor]) -> list[str]:
    return [
        "Thought: The answer is further down.\nAction: scroll(direction='down')",
        "Thought: There it is.\nAction: finished(content='42')",
    ]


_AGENTS = {
    "browser_use": (BrowserUseAgent, _browser_use_replies),
    "skyvern": (SkyvernAgent, _skyvern_replies),
    "ui_tars": (UITarsAgent, _ui_tars_replies),
}


async def _drive(agent_kind: str, *, reasoning: str | None) -> tuple[_Endpoint, _Recording, Any]:
    agent_cls, replies_for = _AGENTS[agent_kind]
    agent = agent_cls()
    agent.memory = SimpleMemory(max_messages=100)  # wired by the Composer in a real run
    capability = BrowserCapability()
    negotiated = negotiate_action(agent, _ActionsOnlyEnv(capability))
    descriptors = [
        a.descriptor() for a in capability.actions if a.action_id in negotiated.active_actions
    ]
    env = _Env2(_observation())
    space, act_fn = build_guarded_act(env.act, descriptors, bindings=negotiated.action_bindings)
    agent.memory.add_messages(
        agent.prepare(
            task="Find the answer",
            action_space=space,
            execution_limits=ReactLoop().execution_limits(),
        )
    )

    endpoint = _Endpoint(replies_for(descriptors), reasoning=reasoning)
    llm = _Recording(endpoint.llm())
    result = await ReactLoop(max_steps=3)._execute(
        agent,
        "Find the answer",
        ctx={
            "env": env,
            "llm": llm,
            "memory": agent.memory,
            "act_fn": act_fn,
            "action_space": space,
            "negotiated_obs": _Negotiated(),
            "task_id": f"reasoning-{agent_kind}",
        },
    )
    assert result.agent_done == AgentDoneStatus.DONE, result.error
    assert env.acted == ["browser.scroll", "browser.done"]
    return endpoint, llm, agent.memory


@pytest.mark.parametrize("agent_kind", sorted(_AGENTS))
async def test_a_shipped_agent_builds_the_same_next_turn(agent_kind: str) -> None:
    with_reasoning, llm, memory = await _drive(agent_kind, reasoning=_THINKING)
    without, _, _ = await _drive(agent_kind, reasoning=None)

    # The provider did hand the Loop the reasoning…
    assert [c.thinking for c in llm.completions] == [_THINKING, _THINKING]
    # …and the turn after it is the one a reply without reasoning gets.
    assert len(with_reasoning.bodies) == 2
    assert with_reasoning.bodies == without.bodies
    _assert_never_sent(with_reasoning.bodies)
    assert all(_THINKING not in str(m.content) for m in memory.get_messages())
