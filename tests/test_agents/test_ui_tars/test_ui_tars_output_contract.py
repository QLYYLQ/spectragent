"""UI-TARS advertises the Loop's action budget — and the Loop runs exactly that.

The shipped templates said ``Action: <one of the action calls below>``,
singular, while the parser accepted several ``Action:`` lines and
``ReactLoop`` truncated past ``max_actions_per_step``. ``UITarsAgent.prepare``
did not even accept the limit, so ``AgentRuntime._loop_policy_for`` —
which hands it to browser_use and skyvern — never could. A recorded run
configured with ``max_actions_per_step: 6`` shows the cost: the task said
"click every matching tile in one step", and the model reasoned "the system
prompt says one of the action calls … I should only output one action",
spending one screenshot round trip per tile until the challenge expired.

Pinned here, in the same two halves as ``test_prompt_matches_loop_limits``:

- the agent renders whatever budget it is handed, in every template, with
  single-action wording at 1 and the multi-action contract above it;
- ``AgentRuntime.execute()`` is what hands it over (probe value 9, which no
  default in play produces);
- and the promise is the Loop's behaviour: a reply over budget runs the
  first N in order, and the rest are reported as not run.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from spectragent.agents.prompts import agent_prompt_variants
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.messages import UserMessage
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.loops.react import ReactLoop
from spectragent.core.registries import collect_registrars
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace
from tests._helpers.action_spaces import browser_descriptors, space_of

#: Resolved from this file, not from the CWD.
SHIPPED_CONFIG = Path(__file__).resolve().parents[3] / "configs" / "browser_use_local.yaml"

_VARIANTS = list(agent_prompt_variants("ui_tars"))

#: The registered descriptors: a UI-TARS verb's points fill the points its
#: action declares, so a hand-built descriptor without them is refused.
_NEGOTIATED = browser_descriptors("click_coordinate", "type_coordinate", "done")

_MAXIMUM = re.compile(r"maximum of (\d+) actions")
_DROPPED = re.compile(r"Anything past the first (\d+) is dropped")
_ACTION_PLACEHOLDER = re.compile(r"^Action: <", re.MULTILINE)


def _prompt(variant: Any, max_actions_per_step: int) -> str:
    # ``Any``: ``_VARIANTS`` is read off disk, so a checker cannot see that
    # its members are the field's Literal (``test_prompt_tree`` asserts it).
    agent = UITarsAgent(prompt_template=variant)
    content = agent.prepare(
        task="t",
        action_space=space_of(_NEGOTIATED),
        execution_limits=ExecutionLimits(max_actions_per_step=max_actions_per_step),
    )[0].content
    assert isinstance(content, str)
    return content


def test_there_are_templates_to_check() -> None:
    """Guards the guard: an empty glob would turn every loop below into a no-op."""
    assert set(_VARIANTS) == {"computer_use", "grounding", "mobile_use"}


@pytest.mark.parametrize("variant", _VARIANTS)
def test_one_action_is_advertised_as_exactly_one(variant: str) -> None:
    prompt = _prompt(variant, 1)
    assert "Write exactly one Action line." in prompt
    assert len(_ACTION_PLACEHOLDER.findall(prompt)) == 1
    # No budget sentence that would invite a second line.
    assert not _MAXIMUM.search(prompt)
    assert "Action line per action" not in prompt


@pytest.mark.parametrize("variant", _VARIANTS)
def test_nine_actions_are_advertised_as_the_multi_action_contract(variant: str) -> None:
    prompt = _prompt(variant, 9)
    assert {int(n) for n in _MAXIMUM.findall(prompt)} == {9}
    assert {int(n) for n in _DROPPED.findall(prompt)} == {9}
    assert "Write one Action line per action, each on its own line." in prompt
    assert "in the order written" in prompt
    assert "you see the screen again only after the last one has run" in prompt
    assert "Nothing written after finished() runs." in prompt
    # The format block shows the repetition, not a singular slot.
    assert len(_ACTION_PLACEHOLDER.findall(prompt)) == 2
    assert "one of the action calls" not in prompt
    assert "Write exactly one Action line." not in prompt


@pytest.mark.parametrize("n", [2, 3, 6])
def test_the_budget_is_whatever_the_agent_is_handed(n: int) -> None:
    prompt = _prompt("computer_use", n)
    assert {int(m) for m in _MAXIMUM.findall(prompt)} == {n}


def test_no_finish_verb_no_promise_about_it() -> None:
    agent = UITarsAgent()
    descriptors = browser_descriptors("click_coordinate")
    content = agent.prepare(
        task="t",
        action_space=space_of(descriptors),
        execution_limits=ExecutionLimits(max_actions_per_step=3),
    )[0].content
    assert isinstance(content, str)
    assert "finished()" not in content


def test_a_budget_below_one_is_a_config_error_not_a_prompt() -> None:
    with pytest.raises(ValueError, match="max_actions_per_step=0"):
        UITarsAgent().prepare(
            task="t",
            action_space=space_of(_NEGOTIATED),
            execution_limits=ExecutionLimits(max_actions_per_step=0),
        )


def test_the_agent_owns_no_copy_of_the_limit() -> None:
    agent = UITarsAgent()
    assert not hasattr(agent, "max_actions_per_step")
    assert not hasattr(agent, "max_steps")


# ── AgentRuntime hands the Loop's limit over ──────────────────────────


class _RecordingMemory:
    """Captures the seed messages ``AgentRuntime`` writes after prepare."""

    supports_protocols = frozenset({"user_assistant"})

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, msg: Any) -> None:
        self.messages.append(msg)

    def add_messages(self, msgs: Any) -> None:
        self.messages.extend(msgs)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


def _ui_tars_config(max_actions_per_step: int) -> dict[str, Any]:
    """The shipped local-Chrome config with its agent swapped for UI-TARS.

    Hooks are dropped so the refused run writes no trace directory into
    the working tree.
    """
    config = yaml.safe_load(SHIPPED_CONFIG.read_text())
    (shipped,) = config["agent"]
    config["agent"] = [
        {
            "agent_id": "ui_tars",
            "id": shipped["id"],
            "agent_loop": {**shipped["agent_loop"], "max_actions_per_step": max_actions_per_step},
            "llm": shipped["llm"],
            "memory": shipped["memory"],
        }
    ]
    config.pop("hook", None)
    return config


@pytest.mark.parametrize("probe", [9, 1])
async def test_runtime_carries_the_loops_limit_into_the_ui_tars_prompt(probe: int) -> None:
    """End to end through ``execute()``: composed agent, real loop, real runtime.

    9 is the discriminating value — the agent's own fallback is 1, so with
    the runtime hand-off removed the prompt would advertise one action while
    the loop runs nine. 1 pins the wording at the boundary.
    """
    run = ConfigComposer(registrars=collect_registrars()).compose(_ui_tars_config(probe))
    agent_id = run.ids("agent")[0]
    agent = run.instance("agent", agent_id)
    assert isinstance(agent, UITarsAgent)
    assert agent.agent_loop.max_actions_per_step == probe

    memory = _RecordingMemory()
    # A duck-typed recorder stands in for the wired memory, as the Composer's
    # setattr would place it; it is not a ``MemoryBase`` subclass.
    agent.memory = memory  # pyright: ignore[reportAttributeAccessIssue]
    runtime = AgentRuntime.for_agent(run, agent_id)

    # The seed prompt is written before env setup, so refusing setup shows
    # the real prompt without launching Chrome or calling an LLM.
    env = runtime.env
    assert env is not None

    async def _refuse(_context: Any = None, **_kw: Any) -> None:
        raise RuntimeError("env deliberately not started by this test")

    env.setup = _refuse  # type: ignore[method-assign]
    result = await runtime.execute("tick the checkbox", task_id="t-1", raise_on_error=False)
    assert result.agent_done.value == "error"

    system_prompt = memory.messages[0].content
    assert isinstance(system_prompt, str)
    if probe == 1:
        assert "Write exactly one Action line." in system_prompt
        assert not _MAXIMUM.search(system_prompt)
    else:
        assert {int(n) for n in _MAXIMUM.findall(system_prompt)} == {probe}, (
            "AgentRuntime is not carrying the loop's limit to UITarsAgent.prepare"
        )


# ── ... and the Loop runs exactly what was advertised ────────────────


async def test_an_over_budget_reply_runs_the_first_n_in_order_and_reports_the_rest() -> None:
    """The prompt's promise, as ReactLoop keeps it, for a UI-TARS reply."""
    loop = ReactLoop()
    loop.max_steps = 1
    loop.max_actions_per_step = 2

    agent = UITarsAgent()
    memory = _RecordingMemory()
    memory.add_messages(
        agent.prepare(
            task="t",
            action_space=space_of(_NEGOTIATED),
            execution_limits=ExecutionLimits(max_actions_per_step=loop.max_actions_per_step),
        )
    )
    assert "maximum of 2 actions" in memory.messages[0].content

    class _Env:
        async def observe(self, _negotiated: Any) -> Any:
            return SimpleNamespace(
                platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
                task=SimpleNamespace(user_request="t"),
                history=SimpleNamespace(items=[]),
                screenshot=ScreenshotObs(
                    image_bytes=b"\x89PNG\r\n\x1a\n",
                    viewport_width=1280,
                    viewport_height=720,
                    image_width=1280,
                    image_height=720,
                ),
            )

    class _LLM:
        inference_kind = "chat"
        model = "stub"

        async def ainvoke(self, _messages: Any, **_kw: Any) -> Any:
            return SimpleNamespace(
                completion=(
                    "Thought: three tiles\n"
                    "Action: click(point='(10, 20)')\n"
                    "Action: click(point='(30, 40)')\n"
                    "Action: click(point='(50, 60)')"
                ),
                usage=None,
            )

        name = "test-model"

        async def aclose(self, *, timeout=5.0):
            return ()

    ran: list[Action] = []

    async def _act(action: Action) -> ActionResult:
        ran.append(action)
        return ActionResult(success=True)

    ctx = {
        "env": _Env(),
        "llm": _LLM(),
        "memory": memory,
        "act_fn": _act,
        "action_space": ActionSpace(),
    }
    await loop._execute(agent, "t", ctx={**ctx, "step_executor": loop})

    assert [(a.params["coordinate_x"], a.params["coordinate_y"]) for a in ran] == [
        (10, 20),
        (30, 40),
    ]
    notes = [
        m.content
        for m in memory.messages
        if isinstance(m, UserMessage) and isinstance(m.content, str)
    ]
    assert any("Only the first 2 action(s)" in note for note in notes)
