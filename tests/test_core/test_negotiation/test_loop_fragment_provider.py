"""The Loop provides the loop-source fragments it builds — and only those are on offer.

``docs/architecture_design.md`` §4.4 "协商规则": the negotiation universe is the
environment's fragments plus the loop-source fragments the selected Loop
declares it builds (``__loop_fragments__``; ``AgentLoopBase`` declares the four
the shared ``loop_fragment_builders`` builds). An agent that requires a
registered loop-source fragment the Loop does not build is refused at
``prepare()``, naming the fragment and the Loop — it used to pass negotiation
and fail at the first step on a missing builder, ending the run as an
environment failure.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from spectragent.core.loops import AgentLoopBase, AgentLoopRegistrar, BranchLoop, ReactLoop
from spectragent.core.loops._loop_fragments import LOOP_FRAGMENT_KEYS, loop_fragment_builders
from spectragent.core.negotiation import IncompatibleFragmentError
from spectragent.core.negotiation.observation import (
    _observation_rules,
    loop_fragments,
    negotiate_observation,
)
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.observation import fragments as _  # noqa: F401 — registers loop.*
from tests._helpers.archive_fakes import (
    ContractArchiveLLM,
    ContractArchiveMemory,
    make_archive_env,
)
from tests._helpers.assembly import AGENT_ID, make_run


class _TaskOnlyLoop(ReactLoop):
    """A ReAct loop that builds the task and nothing else."""

    __registry_key__ = "test_loop_fragments_task_only"
    # A plain set, as a subclass body may write it: frozen when the class is created.
    __loop_fragments__ = {"loop.task"}  # type: ignore[assignment]


def teardown_module() -> None:
    # Idempotent: pytest tears a module down once per contiguous run of its
    # tests, so a selection that interleaves files tears this one down twice.
    if AgentLoopRegistrar.get_or_none("test_loop_fragments_task_only") is not None:
        AgentLoopRegistrar.unregister("test_loop_fragments_task_only")


class _StepAgent:
    """Requires the step budget, takes the history if there is one."""

    __registry_key__ = "step_agent"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["task", "step_info"], ["history"]),
    }

    def build_react_prompt(self, obs: Any) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Any]:
        return []


class _Env:
    provided_fragments: frozenset[str] = frozenset({"browser.screenshot"})


# ── the declaration ───────────────────────────────────────────────────


class TestTheDeclaration:
    def test_the_default_is_what_the_shared_builders_build(self) -> None:
        built = loop_fragment_builders(
            task="t", step=0, max_steps=1, today="2001-02-03", history=(), prev_step=None
        )
        assert frozenset(built) == LOOP_FRAGMENT_KEYS
        assert AgentLoopBase.__loop_fragments__ == LOOP_FRAGMENT_KEYS
        assert ReactLoop.__loop_fragments__ == BranchLoop.__loop_fragments__ == LOOP_FRAGMENT_KEYS

    def test_a_declared_set_is_frozen(self) -> None:
        assert _TaskOnlyLoop.__loop_fragments__ == frozenset({"loop.task"})
        assert isinstance(_TaskOnlyLoop.__loop_fragments__, frozenset)

    def test_a_bare_string_is_refused_where_it_is_written(self) -> None:
        with pytest.raises(TypeError, match="__loop_fragments__ must be a set of fragment keys"):

            class _Typo(AgentLoopBase):
                __abstract__ = True
                __loop_fragments__ = "loop.task"  # type: ignore[assignment]

    def test_an_object_that_declares_nothing_builds_nothing(self) -> None:
        class _Structural:
            __registry_key__ = "structural_loop"

        assert loop_fragments(_Structural()) == frozenset()
        assert loop_fragments(ReactLoop()) == LOOP_FRAGMENT_KEYS


# ── negotiation ───────────────────────────────────────────────────────


class TestNegotiation:
    def test_a_required_fragment_the_loop_does_not_build_names_the_loop(self) -> None:
        with pytest.raises(IncompatibleFragmentError) as info:
            negotiate_observation(_StepAgent(), _TaskOnlyLoop(), _Env())
        error = info.value
        assert error.missing == {"step_info"}
        assert error.provider == "loop 'test_loop_fragments_task_only'"
        assert error.provided == {"loop.task"}
        assert error.requested_by == {"step_info": ("step_agent",)}
        assert str(error).startswith(
            "Required observation fragments not provided by loop 'test_loop_fragments_task_only'."
        )
        assert "Loop 'test_loop_fragments_task_only' provides: ['loop.task']" in str(error)

    def test_an_optional_one_it_does_not_build_is_left_out(self) -> None:
        class _TaskAgent(_StepAgent):
            __wiring__: ClassVar[dict[str, Any]] = {
                "observation": ("observation", ["task"], ["history", "step_info"]),
            }

        result = negotiate_observation(_TaskAgent(), _TaskOnlyLoop(), _Env())
        assert result.loop_sourced == frozenset({"loop.task"})

    def test_every_shipped_loop_builds_what_it_is_asked_for(self) -> None:
        result = negotiate_observation(_StepAgent(), ReactLoop(), _Env())
        assert result.loop_sourced == frozenset({"loop.task", "loop.step_info", "loop.history"})

    def test_a_missing_environment_fragment_is_still_the_environment_s(self) -> None:
        class _DomAgent(_StepAgent):
            __wiring__: ClassVar[dict[str, Any]] = {
                "observation": ("observation", ["task", "dom"]),
            }

        with pytest.raises(IncompatibleFragmentError) as info:
            negotiate_observation(_DomAgent(), _TaskOnlyLoop(), _Env())
        assert info.value.missing == {"dom"} and info.value.provider == "environment"

    def test_the_bare_rules_without_a_loop_offer_every_registered_one(self) -> None:
        result = _observation_rules(
            agent=({"task", "step_info"}, set()),
            loop=(set(), set()),
            env_provides=set(),
        )
        assert result.loop_sourced == frozenset({"loop.task", "loop.step_info"})


class TestPrepare:
    def test_prepare_refuses_it_before_anything_runs(self) -> None:
        run = make_run(
            agent=_StepAgent(),
            loop=_TaskOnlyLoop(max_steps=1),
            env=make_archive_env(),
            llm=ContractArchiveLLM(),
            memory=ContractArchiveMemory(),
        )
        with pytest.raises(IncompatibleFragmentError, match="step_info"):
            AgentRuntime.for_agent(run, AGENT_ID).prepare()
