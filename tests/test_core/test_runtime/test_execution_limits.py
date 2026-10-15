"""Runtime rejects mismatched execution limits before setup or prompt building."""

from dataclasses import FrozenInstanceError

import pytest

from spectragent.core.loops import ReactLoop
from spectragent.core.loops.limits import ExecutionLimits, IncompatibleExecutionLimitsError
from spectragent.core.runtime import AgentRuntime
from tests._helpers.assembly import AGENT_ID
from tests.test_core.test_runtime.test_agent_runtime import SimpleAgent, _make_env, _mk


class LimitConsumer(SimpleAgent):
    __execution_limit_fields__ = frozenset({"max_actions_per_step"})

    def prepare(self, *, task, action_descriptors, execution_limits: ExecutionLimits):
        return [execution_limits]


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
    assert runtime.step_records == []


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
        def prepare(self, *, task, action_descriptors, **kwargs):
            return []

    with pytest.raises(IncompatibleExecutionLimitsError, match="explicitly accept"):
        runtime_for(Sink(), ReactLoop()).prepare()
