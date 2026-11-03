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

    def prepare(self, *, task, action_space, execution_limits: ExecutionLimits):
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
