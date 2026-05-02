"""Tests for loop-source fragment behaviour in the dynamic observation model.

Loop-source fragments (``__source__ = "loop"``) must be Optional with
default=None in the generated NegotiatedObservation: env.observe() doesn't
fill them, the Loop does, and the model needs to be constructable from
just the env-source half.
"""

import pytest

# Ensure all fragments are registered.
from spectragent.schemas.observation import fragments as _  # noqa: F401
from spectragent.schemas.observation.dynamic_model import build_observation_model
from spectragent.schemas.observation.fragments import (
    ActionResultObs,
    HistoryObs,
    StepInfoObs,
    TaskObs,
    TerminalObs,
)


class TestLoopFragmentOptional:
    """Loop-source fields default to None; env-source remain required."""

    def test_loop_fragment_default_none(self) -> None:
        Model = build_observation_model(frozenset({"loop.task"}))
        obs = Model()
        assert obs.task is None  # type: ignore[attr-defined]

    def test_env_fragment_still_required(self) -> None:
        Model = build_observation_model(frozenset({"docker.terminal"}))
        with pytest.raises(Exception):  # noqa: B017 — pydantic raises ValidationError
            Model()

    def test_mixed_env_required_and_loop_optional(self) -> None:
        Model = build_observation_model(
            frozenset({"docker.terminal", "loop.task", "loop.step_info"})
        )
        # Env-source must be passed; loop-source can be omitted.
        obs = Model(terminal=TerminalObs(stdout="ok", cwd="/"))
        assert obs.terminal.stdout == "ok"  # type: ignore[attr-defined]
        assert obs.task is None  # type: ignore[attr-defined]
        assert obs.step_info is None  # type: ignore[attr-defined]

    def test_loop_fragment_can_still_be_passed(self) -> None:
        Model = build_observation_model(frozenset({"loop.task"}))
        obs = Model(task=TaskObs(user_request="navigate to example.com"))
        assert obs.task.user_request == "navigate to example.com"  # type: ignore[attr-defined]


class TestNewLoopFragments:
    """The three new loop-source fragment shapes."""

    def test_task_obs_minimal(self) -> None:
        t = TaskObs(user_request="hello world")
        assert t.user_request == "hello world"
        assert t.follow_ups == []

    def test_task_obs_follow_ups(self) -> None:
        t = TaskObs(user_request="initial", follow_ups=["second", "third"])
        assert t.follow_ups == ["second", "third"]

    def test_step_info_obs(self) -> None:
        s = StepInfoObs(step=3, max_steps=20, today="2026-05-01")
        assert s.step == 3
        assert s.max_steps == 20
        assert s.today == "2026-05-01"

    def test_history_obs_default_empty(self) -> None:
        h = HistoryObs()
        assert h.items == []

    def test_history_obs_with_items(self) -> None:
        from spectragent.schemas.step_record import StepRecord

        record = StepRecord(step=0, llm_output="thinking…")
        h = HistoryObs(items=[record])
        assert len(h.items) == 1
        assert h.items[0].step == 0

    def test_action_result_obs_still_present(self) -> None:
        # Ensure the existing loop-source fragment is unaffected.
        a = ActionResultObs(last_action="click", success=True)
        assert a.last_action == "click"
        assert a.error is None
