"""Runtime rejects mismatched execution limits before setup or prompt building."""

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from spectragent.core.loops import ReactLoop
from spectragent.core.loops.limits import ExecutionLimits, IncompatibleExecutionLimitsError
from spectragent.core.runtime import AgentRuntime
from tests._helpers.assembly import AGENT_ID
from tests.test_core.test_runtime.test_agent_runtime import SimpleAgent, _make_env, _mk


class LimitConsumer(SimpleAgent):
    __execution_limit_fields__ = frozenset({"max_actions_per_step"})

    def prepare(self, *, task, action_space, execution_limits: ExecutionLimits):
        return [execution_limits]


class SettingsChecker(SimpleAgent):
    """Reads the Loop's cap in ``check_run_settings`` only: its ``prepare`` takes no limits."""

    __execution_limit_fields__ = frozenset({"max_actions_per_step"})

    def __init__(self) -> None:
        self.asked: list[ExecutionLimits] = []

    def check_run_settings(self, execution_limits: ExecutionLimits) -> None:
        self.asked.append(execution_limits)
        limit = execution_limits.max_actions_per_step
        if limit is None or limit < 2:
            raise ValueError(f"needs the Loop's max_actions_per_step at 2 or more; it is {limit}")

    def prepare(self, *, task, action_space):
        return ["seed"]


def runtime_for(agent, loop):
    return AgentRuntime.for_agent(_mk(agent, loop, _make_env()), AGENT_ID)


def test_the_loop_limits_are_immutable_and_reach_the_existing_seed_slot():
    limits = ExecutionLimits(max_actions_per_step=7)

    class Loop(ReactLoop):
        def execution_limits(self):
            return limits

    runtime = runtime_for(LimitConsumer(), Loop())
    runtime.prepare()
    assert runtime.seed_messages("task") == [limits]
    assert runtime.seed_messages("task")[0] is limits
    field = "max_actions_per_step"
    with pytest.raises(FrozenInstanceError):
        setattr(limits, field, 5)


def test_missing_consumption_declaration_is_refused_before_setup():
    class Undeclared:
        build_react_prompt = SimpleAgent.build_react_prompt
        parse_react_output = SimpleAgent.parse_react_output

    runtime = runtime_for(Undeclared(), ReactLoop())
    with pytest.raises(IncompatibleExecutionLimitsError, match="declare.*explicitly"):
        runtime.prepare()


def test_explicit_empty_consumption_needs_no_prepare_slot():
    runtime_for(SimpleAgent(), ReactLoop(max_actions_per_step=8)).prepare()


def test_missing_limit_is_refused_during_prepare():
    class NoLimitLoop(ReactLoop):
        def execution_limits(self):
            return ExecutionLimits()

    with pytest.raises(IncompatibleExecutionLimitsError, match="does not provide.*max_actions"):
        runtime_for(LimitConsumer(), NoLimitLoop()).prepare()


def test_unknown_consumed_field_is_refused():
    class Misspelled(LimitConsumer):
        __execution_limit_fields__ = frozenset({"max_action_per_step"})

    with pytest.raises(IncompatibleExecutionLimitsError, match="unknown.*max_action_per_step"):
        runtime_for(Misspelled(), ReactLoop()).prepare()


def test_a_keyword_sink_is_not_an_explicit_limits_parameter():
    class Sink(LimitConsumer):
        def prepare(self, *, task, action_space, **kwargs):
            return []

    with pytest.raises(IncompatibleExecutionLimitsError, match="explicitly accept"):
        runtime_for(Sink(), ReactLoop()).prepare()


def test_react_provides_its_parse_settings_with_none_for_no_limit_on_unread_replies():
    """M3A checks these: AndroidWorld never re-asks, and never ends a run on an unread reply."""

    class ParseConsumer(LimitConsumer):
        __execution_limit_fields__ = frozenset(
            {"parse_retries_per_step", "max_consecutive_parse_errors"}
        )

    loop = ReactLoop(parse_retries_per_step=2, max_consecutive_parse_errors=None)
    runtime = runtime_for(ParseConsumer(), loop)
    runtime.prepare()

    (limits,) = runtime.seed_messages("task")
    assert (limits.parse_retries_per_step, limits.max_consecutive_parse_errors) == (2, None)
    assert limits.provided() == ExecutionLimits.limit_names()


def test_a_limit_left_none_is_provided_only_when_named_unbounded():
    unbounded = frozenset({"max_consecutive_parse_errors"})

    assert "max_consecutive_parse_errors" not in ExecutionLimits(max_actions_per_step=1).provided()
    assert "max_consecutive_parse_errors" in ExecutionLimits(unbounded=unbounded).provided()
    with pytest.raises(ValueError, match="unbounded names only limits left None, not max_c"):
        ExecutionLimits(max_consecutive_parse_errors=3, unbounded=unbounded)
    with pytest.raises(ValueError, match="unbounded names only limits left None, not max_steps"):
        ExecutionLimits(unbounded=frozenset({"max_steps"}))


def test_run_settings_an_agent_refuses_are_refused_by_prepare_before_anything_starts():
    """``RunSettingsCheck``: the refusal comes out of ``prepare()``, so ``config check`` and the
    bench pre-flight see it; ``execute()`` neither negotiates, sets up nor seeds."""
    agent = SettingsChecker()
    runtime = runtime_for(agent, ReactLoop(max_actions_per_step=1))
    env = runtime.env
    assert env is not None
    setups: list[object] = []

    async def setup(context=None, **kwargs):
        setups.append(context)

    env.setup = setup

    with pytest.raises(ValueError, match="max_actions_per_step at 2 or more; it is 1"):
        runtime.prepare()
    with pytest.raises(ValueError, match="max_actions_per_step at 2 or more; it is 1"):
        asyncio.run(runtime.execute("task", raise_on_error=False))

    assert [limits.max_actions_per_step for limits in agent.asked] == [1, 1]
    assert runtime.negotiated_observation is None and setups == []


def test_limits_the_check_alone_consumes_are_asked_for_and_not_handed_to_prepare():
    agent = SettingsChecker()
    runtime = runtime_for(agent, ReactLoop(max_actions_per_step=2))

    runtime.prepare()

    assert [limits.max_actions_per_step for limits in agent.asked] == [2]
    assert runtime.seed_messages("task") == ["seed"]
