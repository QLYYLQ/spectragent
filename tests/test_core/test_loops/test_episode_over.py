"""The environment may end a run: after each policy step the Loop asks whether the episode is over.

The user's ruling of 2026-11-03 (``docs/architecture_design.md`` §4.1).
AndroidWorld's runner asks a MiniWoB++ page after every step whether it
reported a reward and ends the episode when it has. ``ReactLoop`` asks its
environment (``env.episode_over``) after every policy step that did not end the
run. Over: the run ends as ``episode_over``, apart from the agent's ``done`` and
from running out of steps, with the terminal observation a normal ending takes.
A question that fails ends the run as an environment failure, without one. A
step that ended the run with ``loop.done`` is not followed by the question.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.core.loops.errors import RunAbort
from spectragent.core.loops.react import ReactLoop
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace


class _Completion:
    completion = "reply"
    usage = None


class _LLM:
    inference_kind = "chat"
    model = "test-model"

    async def ainvoke(self, messages: Any, **kw: Any) -> Any:
        return _Completion()


class _Memory:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, m: Any) -> None:
        self.messages.append(m)

    def add_messages(self, ms: Any) -> None:
        self.messages.extend(ms)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


class _Env:
    """Says whether the episode is over from *answers*, one per question (an exception is raised)."""

    def __init__(self, answers: Sequence[object]) -> None:
        self.answers = list(answers)
        self.asked = 0
        self.observed = 0

    async def episode_over(self) -> bool:
        answer = self.answers[min(self.asked, len(self.answers) - 1)]
        self.asked += 1
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, bool)
        return answer

    async def observe(self, negotiated: Any) -> Any:
        self.observed += 1
        return object()


class _Negotiated:
    loop_sourced = frozenset()
    env_sourced = frozenset()


class _Agent:
    """Replies with the next planned batch; the last one repeats."""

    __execution_limit_fields__ = frozenset()

    def __init__(self, plan: list[list[Action]]) -> None:
        self._plan = plan
        self._i = 0

    async def build_react_prompt(self, obs: Any, *, context: Any) -> list[Any]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Action]:
        actions = self._plan[min(self._i, len(self._plan) - 1)]
        self._i += 1
        return list(actions)

    def get_react_output_model(self) -> None:
        return None


def _click(index: int) -> Action:
    return Action(name="android.click", params={"index": index})


_DONE = Action(name="loop.done", params={"extracted_content": "the answer"})


async def _act(action: Action) -> ActionResult:
    if action.name == "loop.done":
        return ActionResult(success=True, is_done=True, extracted_content="the answer")
    if action.params.get("index") == 0:
        return ActionResult(success=False, error="element 0 is covered")
    return ActionResult(success=True, extracted_content=f"clicked {action.params['index']}")


async def _run(
    plan: list[list[Action]], env: _Env, *, max_steps: int = 3, terminal: bool = False
) -> tuple[Any, StepRecorder]:
    recorder = StepRecorder()
    loop = ReactLoop(max_steps=max_steps, max_actions_per_step=2, terminal_observation=terminal)
    result = await loop._execute(
        _Agent(plan),
        "Follow the instructions shown on the top of the screen: Click on Submit.",
        ctx={
            "env": env,
            "llm": _LLM(),
            "memory": _Memory(),
            "act_fn": _act,
            "action_space": ActionSpace(),
            "negotiated_obs": _Negotiated(),
            "step_recorder": recorder,
            "task_id": "t-1",
        },
    )
    return result, recorder


async def test_the_run_ends_when_the_environment_says_the_episode_is_over() -> None:
    env = _Env([False, True])
    result, recorder = await _run([[_click(1)], [_click(2)], [_DONE]], env)

    assert result.agent_done.value == "episode_over"
    assert result.env_status.value == "success"
    assert result.agent_reported_success is None, "the agent made no claim"
    assert result.answer == "clicked 2", "the best answer seen, as on max_steps"
    assert result.error is None
    assert env.asked == 2, "once after each step"
    assert [r.step for r in recorder.records] == [0, 1]
    assert result.metrics.steps == 2


async def test_an_episode_that_ends_on_the_last_step_is_not_a_run_out_of_steps() -> None:
    """As AndroidWorld's runner: it asks after the last step too."""
    result, _ = await _run([[_click(1)], [_click(2)]], _Env([False, True]), max_steps=2)
    assert result.agent_done.value == "episode_over"


async def test_a_run_whose_episode_never_ends_runs_out_of_steps() -> None:
    env = _Env([False])
    result, _ = await _run([[_click(1)]], env, max_steps=2)
    assert result.agent_done.value == "max_steps"
    assert env.asked == 2


async def test_a_step_that_ended_the_run_with_done_is_not_followed_by_the_question() -> None:
    """The agent ended it first, within its step; the environment is not asked."""
    env = _Env([True])
    result, _ = await _run([[_click(1), _DONE]], env)
    assert result.agent_done.value == "done"
    assert env.asked == 0


async def test_the_question_follows_a_step_whose_action_failed() -> None:
    env = _Env([False, True])
    result, recorder = await _run([[_click(0), _click(1)], [_click(2)]], env)
    assert recorder.records[0].skipped_actions == 1
    assert (result.agent_done.value, env.asked) == ("episode_over", 2)


async def test_an_episode_over_ending_takes_the_terminal_observation() -> None:
    env = _Env([True])
    result, recorder = await _run([[_click(1)]], env, terminal=True)
    assert result.agent_done.value == "episode_over"
    assert [r.origin for r in recorder.records] == ["policy", "terminal"]


async def test_a_question_that_fails_ends_the_run_as_an_environment_failure() -> None:
    env = _Env([ConnectionError("the service is gone")])
    result, recorder = await _run([[_click(1)]], env, terminal=True)

    assert (result.agent_done.value, result.env_status.value) == ("error", "failed")
    assert result.error == (
        "asking the environment whether the episode is over after step 0 failed: "
        "ConnectionError: the service is gone"
    )
    assert [r.origin for r in recorder.records] == ["policy"], "no terminal observation"
    assert recorder.records[0].error_phase == "", "the step it followed stands as recorded"


async def test_a_veto_from_the_question_propagates() -> None:
    with pytest.raises(RunAbort, match="stop"):
        await _run([[_click(1)]], _Env([RunAbort("stop")]))


async def test_a_run_without_an_environment_has_no_episode_to_end() -> None:
    run: Any = SimpleNamespace(env=None)
    assert await ReactLoop().end_if_episode_over(run) is None
