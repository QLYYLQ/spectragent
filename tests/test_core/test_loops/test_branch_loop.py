"""``BranchLoop``: restore a recorded run to its checkpoint, decide step *t* again, continue.

``docs/architecture_design.md`` §4.1 "`BranchLoop`". Sources are recorded
through the real ``AgentRuntime`` + ``ReactLoop`` + ``TraceWriter``
(``tests/_helpers/branch_fakes.py``); branches run through the same runtime
with a scripted teacher. Pinned here: the step budget (``continue_steps`` 0
and 2), what restored, policy and terminal records hold, strict vs warn
divergence, vetoes in every phase, statelessness, every way a checkpoint is
refused before anything is set up, and the ``branch_instruction`` the teacher
is given at step *t*.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest

from spectragent.core.loops import BranchLoop, ReactLoop
from spectragent.core.loops.errors import RunAbort
from spectragent.core.loops.restore import PrefixReplayRestorer
from spectragent.core.loops.validation import (
    IncompatibleCheckpointError,
    IncompatibleLoopInputError,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.reconstruction import recorded_form
from spectragent.core.trace.redaction import OMITTED
from spectragent.schemas.observation.fragments import StepInfoObs
from spectragent.schemas.restoration import BranchProvenance
from tests._helpers.branch_fakes import (
    CLICK,
    DONE,
    PAGE_KEY,
    BranchFakeAgent,
    BranchFakeEnv,
    make_cap,
    record_source,
    run_branch,
)

#: A four-step source: three clicks, then done.
SOURCE_REPLIES = ["click 100", "click 101", "click 102", "done ok"]


@pytest.fixture
async def source(tmp_path: Path) -> Path:
    return await record_source(tmp_path, SOURCE_REPLIES)


def _origins(run_dir: Path | None) -> list[tuple[int, str]]:
    assert run_dir is not None
    return [(r.step, r.origin) for r in RunTrace(run_dir).step_records]


class TestBudget:
    async def test_continue_steps_zero(self, tmp_path: Path, source: Path) -> None:
        out = await run_branch(tmp_path, source, 2, ["click 102"])
        assert out.result is not None
        assert out.result.agent_done.value == "max_steps"
        assert out.result.metrics.steps == 1
        assert out.result.metrics.llm_calls == 1
        assert len(out.llm.prompts) == 1
        assert _origins(out.run_dir) == [
            (0, "restored"),
            (1, "restored"),
            (2, "policy"),
            (3, "terminal"),
        ]

    async def test_continue_steps_two(self, tmp_path: Path, source: Path) -> None:
        out = await run_branch(
            tmp_path,
            source,
            1,
            ["click 102", "click 100", "click 101"],
            loop=BranchLoop(continue_steps=2),
        )
        assert out.result is not None
        assert out.result.agent_done.value == "max_steps"
        assert out.result.metrics.steps == 3
        assert len(out.llm.prompts) == 3
        assert _origins(out.run_dir) == [
            (0, "restored"),
            (1, "policy"),
            (2, "policy"),
            (3, "policy"),
            (4, "terminal"),
        ]

    async def test_max_steps_caps_the_budget(self, tmp_path: Path, source: Path) -> None:
        out = await run_branch(
            tmp_path, source, 1, ["click 102"], loop=BranchLoop(continue_steps=5, max_steps=1)
        )
        assert out.result is not None
        assert out.result.metrics.steps == 1
        assert len(out.llm.prompts) == 1

    async def test_done_in_the_policy_step_ends_with_a_terminal_observation(
        self, tmp_path: Path, source: Path
    ) -> None:
        out = await run_branch(
            tmp_path, source, 1, ["done the kettle"], loop=BranchLoop(continue_steps=3)
        )
        assert out.result is not None
        assert out.result.agent_done.value == "done"
        assert out.result.answer == "the kettle"
        assert _origins(out.run_dir) == [(0, "restored"), (1, "policy"), (2, "terminal")]

    async def test_a_checkpoint_at_step_zero_restores_nothing(
        self, tmp_path: Path, source: Path
    ) -> None:
        out = await run_branch(tmp_path, source, 0, ["click 101"])
        assert out.result is not None
        assert _origins(out.run_dir) == [(0, "policy"), (1, "terminal")]
        trace = RunTrace(out.run_dir)  # type: ignore[arg-type]
        assert trace.step(0).restoration is not None  # type: ignore[union-attr]

    async def test_terminal_observation_can_be_turned_off(
        self, tmp_path: Path, source: Path
    ) -> None:
        out = await run_branch(
            tmp_path, source, 1, ["click 102"], loop=BranchLoop(terminal_observation=False)
        )
        assert _origins(out.run_dir) == [(0, "restored"), (1, "policy")]


class TestRecords:
    async def test_restored_records_keep_actual_facts_and_source_material(
        self, tmp_path: Path, source: Path
    ) -> None:
        out = await run_branch(tmp_path, source, 2, ["click 102"])
        recorded = RunTrace(source)
        branch = RunTrace(out.run_dir)  # type: ignore[arg-type]
        for step in (0, 1):
            mine, theirs = branch.step(step), recorded.step(step)
            assert mine is not None and theirs is not None
            assert mine.origin == "restored"
            assert (mine.actions, mine.action_results) == (theirs.actions, theirs.action_results)
            assert mine.llm_output == ""
            assert mine.source_history is not None
            assert mine.source_history.source_reply == theirs.llm_output
            assert mine.observation_summary == theirs.observation_summary
            # The source's tokens are not this run's.
            assert theirs.token_usage is not None
            assert mine.token_usage is not None and mine.token_usage.total_tokens == 0
            assert mine.usage_coverage is not None and mine.usage_coverage.calls == 0
            assert mine.timestamp > theirs.timestamp
            # Re-measured by the recorder, not copied: no model call here.
            assert mine.llm_latency_ms in (None, 0)
            assert mine.restoration is not None
            assert mine.restoration.verdict == "ok"
            assert [a.method for a in mine.restoration.actions] == ["geometry_signature"]

    async def test_restored_steps_commit_their_live_observation(self, tmp_path: Path) -> None:
        source = await record_source(tmp_path, SOURCE_REPLIES)
        cap = make_cap(id_offset=500)  # a relaunched world: fresh element ids
        out = await run_branch(tmp_path, source, 2, ["click 502"], cap=cap)
        branch = RunTrace(out.run_dir)  # type: ignore[arg-type]
        for step in (0, 1):
            page = branch.observation_fragment(step, PAGE_KEY)
            assert page is not None
            assert page.elements == [500, 501, 502]  # type: ignore[union-attr]
            # Env fragments only: the loop's own were not rebuilt for it.
            assert branch.observation_fragment(step, "loop.history") is None
        # The restorer executed the retargeted actions.
        assert cap.log[:2] == [f"{CLICK}({{'index': 500}})", f"{CLICK}({{'index': 501}})"]
        assert out.result is not None and out.result.agent_done.value == "max_steps"

    async def test_the_policy_record_carries_the_checkpoint_check(
        self, tmp_path: Path, source: Path
    ) -> None:
        out = await run_branch(tmp_path, source, 2, ["click 102"])
        record = RunTrace(out.run_dir).step(2)  # type: ignore[arg-type]
        assert record is not None and record.origin == "policy"
        assert record.restoration is not None
        assert record.restoration.step == 2
        assert record.restoration.actions == []
        assert [e.url_match for e in record.restoration.evidence] == [True]

    async def test_step_info_is_pinned_to_the_source(self, tmp_path: Path, source: Path) -> None:
        """The source ran ``max_steps=10`` on 2001-02-03; the branch loop says neither."""
        out = await run_branch(tmp_path, source, 2, ["click 102"], loop=BranchLoop(max_steps=50))
        info = RunTrace(out.run_dir).observation_fragment(2, "loop.step_info")  # type: ignore[arg-type]
        assert isinstance(info, StepInfoObs)
        assert (info.step, info.max_steps, info.today) == (3, 10, "2001-02-03")

    async def test_today_override_wins(self, tmp_path: Path, source: Path) -> None:
        out = await run_branch(
            tmp_path, source, 2, ["click 102"], loop=BranchLoop(today_override="2030-01-01")
        )
        info = RunTrace(out.run_dir).observation_fragment(2, "loop.step_info")  # type: ignore[arg-type]
        assert isinstance(info, StepInfoObs)
        assert (info.max_steps, info.today) == (10, "2030-01-01")

    async def test_metrics_history_and_answer(self, tmp_path: Path, source: Path) -> None:
        out = await run_branch(
            tmp_path, source, 2, ["oops", "oops"], loop=BranchLoop(max_consecutive_parse_errors=5)
        )
        result = out.result
        assert result is not None
        assert result.agent_done.value == "max_steps"
        assert result.metrics.steps == 1
        assert result.action_history == []  # the restored prefix is not this run's
        # The best answer so far is the source's: its restored results carried it.
        assert result.answer == "clicked 1"

    async def test_policy_actions_are_the_runs_own(self, tmp_path: Path, source: Path) -> None:
        out = await run_branch(tmp_path, source, 2, ["click 102"])
        assert out.result is not None
        assert out.result.action_history == [f"{CLICK}({{'index': 102}})"]

    async def test_the_teacher_sees_the_sources_context(self, tmp_path: Path, source: Path) -> None:
        out = await run_branch(tmp_path, source, 2, ["click 102"])
        assert recorded_form(out.llm.prompts[0]) == recorded_form(
            RunTrace(source).llm_messages(2)[0].messages
        )


class TestHistorySource:
    def test_source_is_the_default(self) -> None:
        assert BranchLoop().history_source == "source"

    @pytest.mark.parametrize("verify", ["strict", "warn"])
    async def test_restored_history_uses_actual_targets_results_and_source_labels(
        self, tmp_path: Path, source: Path, verify: Literal["strict", "warn"]
    ) -> None:
        loop = BranchLoop(history_source="restored")
        loop.restorer = PrefixReplayRestorer(verify=verify)
        agent = BranchFakeAgent()
        out = await run_branch(
            tmp_path, source, 2, ["done ok"], cap=make_cap(id_offset=500), loop=loop, agent=agent
        )
        assert out.result is not None and out.result.agent_done.value == "done"
        text = str(recorded_form(out.llm.prompts[0]))
        assert "[restored]" in text and "[source" in text
        assert "index': 500" in text and "clicked 0" in text
        assert "restored branchfake.click" in text
        assert agent.parsed_log[0].startswith("restored ")
        assert out.run_dir is not None
        trace = RunTrace(out.run_dir)
        first = trace.step(0)
        assert first is not None and first.llm_output == ""
        assert first.actions[0].params == {"index": 500}
        assert first.source_history is not None
        assert first.source_history.source_actions[0].params == {"index": 100}
        assert trace.llm_messages(0) == []
        assert out.result.metrics.llm_calls == 1
        assert out.cap.cycle == 4  # prefix, live checkpoint, terminal; no validation-only capture

    async def test_restored_requires_a_public_async_history_slot(
        self, tmp_path: Path, source: Path
    ) -> None:
        agent: Any = BranchFakeAgent()
        agent.build_restored_history = None
        with pytest.raises(Exception, match="build_restored_history"):
            await run_branch(
                tmp_path,
                source,
                1,
                ["done ok"],
                agent=agent,
                loop=BranchLoop(history_source="restored"),
            )


@pytest.mark.parametrize("history_source", ["source", "restored"])
@pytest.mark.parametrize("verify", ["strict", "warn"])
async def test_history_choice_does_not_change_environment_verification(
    tmp_path: Path,
    source: Path,
    history_source: Literal["source", "restored"],
    verify: Literal["strict", "warn"],
) -> None:
    loop = BranchLoop(history_source=history_source)
    loop.restorer = PrefixReplayRestorer(verify=verify)
    out = await run_branch(
        tmp_path,
        source,
        2,
        ["done ok"],
        loop=loop,
        cap=make_cap(urls={2: "https://diverged.test/"}),
    )
    assert out.result is not None
    assert out.result.agent_done.value == ("done" if verify == "warn" else "error")
    assert out.run_dir is not None
    record = RunTrace(out.run_dir).step(1)
    assert record is not None and record.restoration is not None
    assert record.restoration.verdict == ("warn" if verify == "warn" else "diverged")


async def test_restored_history_refuses_missing_actual_results_even_under_warn(
    tmp_path: Path, source: Path
) -> None:
    from dataclasses import replace

    class MissingResultRestorer:
        restorer_id = "missing_result"

        async def restore(self, ctx):
            async for item in PrefixReplayRestorer(verify="warn").restore(ctx):
                if item.step == 0:
                    checks = [
                        action.model_copy(update={"result": None}) for action in item.check.actions
                    ]
                    item = replace(item, check=item.check.model_copy(update={"actions": checks}))
                yield item

    loop = BranchLoop(history_source="restored")
    loop.restorer = MissingResultRestorer()
    out = await run_branch(tmp_path, source, 2, ["done ok"], loop=loop)
    assert out.result is not None and out.result.agent_done.value == "error"
    assert out.llm.prompts == []
    assert out.run_dir is not None
    record = RunTrace(out.run_dir).step(0)
    assert record is not None and record.error_phase == "restored_history"
    assert record.error is not None and "one actual result" in record.error
    assert record.source_history is not None
    assert record.source_history.source_reply == SOURCE_REPLIES[0]


async def test_restored_history_refuses_missing_source_result_before_setup(
    tmp_path: Path, source: Path
) -> None:
    path = source / "steps/000.json"
    document = json.loads(path.read_text())
    document["action_results"] = []
    path.write_text(json.dumps(document))
    cap = make_cap()
    with pytest.raises(IncompatibleCheckpointError, match="necessary executed action results"):
        await run_branch(
            tmp_path, source, 2, ["done ok"], cap=cap, loop=BranchLoop(history_source="restored")
        )
    assert cap.setups == 0


def _warn_loop() -> BranchLoop:
    """The restorer is a wired receptor, not a constructor argument: set as the Composer would."""
    loop = BranchLoop()
    loop.restorer = PrefixReplayRestorer(verify="warn")
    return loop


class TestDivergence:
    async def test_strict_ends_the_run(self, tmp_path: Path, source: Path) -> None:
        cap = make_cap(urls={2: "https://elsewhere.test/"})  # step 1's page moved
        out = await run_branch(tmp_path, source, 3, ["click 100"], cap=cap)
        result = out.result
        assert result is not None
        assert (result.agent_done.value, result.env_status.value) == ("error", "failed")
        assert result.error is not None and result.error.startswith("RestorationDivergedError")
        trace = RunTrace(out.run_dir)  # type: ignore[arg-type]
        assert [(r.step, r.origin, r.error_phase) for r in trace.step_records] == [
            (0, "restored", ""),
            (1, "restored", "restore"),
        ]
        failing = trace.step(1)
        assert failing is not None and failing.restoration is not None
        assert failing.restoration.verdict == "diverged"
        assert failing.restoration.reasons == ["url_mismatch"]
        # Nothing was decided and nothing ran on the diverged page.
        assert out.llm.prompts == []
        assert cap.log == [f"{CLICK}({{'index': 100}})"]
        assert result.metrics.steps == 0

    async def test_warn_continues(self, tmp_path: Path, source: Path) -> None:
        cap = make_cap(urls={2: "https://elsewhere.test/"})
        out = await run_branch(
            tmp_path,
            source,
            3,
            ["done anyway"],
            cap=cap,
            loop=_warn_loop(),
        )
        result = out.result
        assert result is not None and result.agent_done.value == "done"
        trace = RunTrace(out.run_dir)  # type: ignore[arg-type]
        checks = {r.step: r.restoration for r in trace.step_records}
        assert checks[1] is not None and checks[1].verdict == "warn"
        assert checks[0] is not None and checks[0].verdict == "ok"
        assert [r.origin for r in trace.step_records] == [
            "restored",
            "restored",
            "restored",
            "policy",
            "terminal",
        ]

    async def test_a_failing_environment_ends_the_run_in_restore(
        self, tmp_path: Path, source: Path
    ) -> None:
        class _DyingEnv(BranchFakeEnv):
            __registry_key__ = "branchfake_dying_env"

            async def observe(self, negotiated: Any) -> Any:
                if self._observation_cycle == 1:
                    raise ConnectionError("CDP websocket closed")
                return await super().observe(negotiated)

        out = await run_branch(tmp_path, source, 2, ["click 102"], env_cls=_DyingEnv)
        result = out.result
        assert result is not None
        assert (result.agent_done.value, result.env_status.value) == ("error", "failed")
        assert result.error is not None
        assert result.error.startswith("restore failed at step 1: ConnectionError")
        assert _origins(out.run_dir) == [(0, "restored"), (1, "restored")]
        record = RunTrace(out.run_dir).step(1)  # type: ignore[arg-type]
        assert record is not None and record.error_phase == "restore"


class _VetoingAgent(BranchFakeAgent):
    """Vetoes from a woven slot on its *n*-th prompt."""

    veto_on_prompt: ClassVar[int] = 1

    async def build_react_prompt(self, obs: Any, *, context) -> list[Any]:
        if self.prompts + 1 == self.veto_on_prompt:
            self.prompts += 1
            raise RunAbort("the page shows a captcha", source="prompt_guard")
        return await super().build_react_prompt(obs, context=context)


class TestVetoes:
    async def test_in_replay(self, tmp_path: Path, source: Path) -> None:
        cap = make_cap()
        with pytest.raises(RunAbort, match="captcha"):
            await run_branch(tmp_path, source, 2, ["click 102"], agent=_VetoingAgent(), cap=cap)
        records = RunTrace(tmp_path / "branches" / "branch-s2").step_records
        assert [(r.step, r.origin, r.error_phase) for r in records] == [(0, "restored", "replay")]
        assert cap.log == []

    async def test_in_restore(self, tmp_path: Path, source: Path) -> None:
        cap = make_cap()
        cap.veto_act_at = 1
        with pytest.raises(RunAbort, match="trap"):
            await run_branch(tmp_path, source, 2, ["click 102"], cap=cap)
        records = RunTrace(tmp_path / "branches" / "branch-s2").step_records
        assert [(r.step, r.origin, r.error_phase) for r in records] == [
            (0, "restored", ""),
            (1, "restored", "restore"),
        ]

    async def test_in_the_policy_step(self, tmp_path: Path, source: Path) -> None:
        cap = make_cap()
        cap.veto_act_at = 2
        with pytest.raises(RunAbort, match="trap"):
            await run_branch(tmp_path, source, 2, ["click 102"], cap=cap)
        records = RunTrace(tmp_path / "branches" / "branch-s2").step_records
        assert [(r.step, r.origin, r.error_phase) for r in records] == [
            (0, "restored", ""),
            (1, "restored", ""),
            (2, "policy", "act"),
        ]

    async def test_on_the_terminal_observation(self, tmp_path: Path, source: Path) -> None:
        class _GuardedEnv(BranchFakeEnv):
            __registry_key__ = "branchfake_guarded_env"

            async def observe(self, negotiated: Any) -> Any:
                if self._observation_cycle == 3:  # restore 0, 1; step 2; then the terminal
                    raise RunAbort("banned page", source="dom_guard")
                return await super().observe(negotiated)

        with pytest.raises(RunAbort, match="banned"):
            await run_branch(tmp_path, source, 2, ["click 102"], env_cls=_GuardedEnv)
        records = RunTrace(tmp_path / "branches" / "branch-s2").step_records
        assert [(r.step, r.origin, r.error_phase) for r in records][-1] == (
            3,
            "terminal",
            "observe",
        )


class TestStateless:
    async def test_one_loop_woven_onto_two_agents(self, tmp_path: Path, source: Path) -> None:
        loop = BranchLoop(continue_steps=1)
        fields_before = dict(vars(loop))
        first, second = await asyncio.gather(
            run_branch(tmp_path, source, 1, ["click 102", "done a"], loop=loop, run_id="b-one"),
            run_branch(tmp_path, source, 2, ["done b"], loop=loop, run_id="b-two"),
        )
        assert first.result is not None and second.result is not None
        assert (first.result.answer, second.result.answer) == ("a", "b")
        assert _origins(first.run_dir)[:2] == [(0, "restored"), (1, "policy")]
        assert _origins(second.run_dir)[:3] == [(0, "restored"), (1, "restored"), (2, "policy")]
        assert dict(vars(loop)) == fields_before
        assert not hasattr(loop, "ctx")


class _OtherReact(ReactLoop):
    __registry_key__ = "test_branch_other_react"


def teardown_module() -> None:
    from spectragent.core.loops import AgentLoopRegistrar

    # Idempotent: pytest tears a module down once per contiguous run of its
    # tests, so a selection that interleaves files tears this one down twice.
    if AgentLoopRegistrar.get_or_none("test_branch_other_react") is not None:
        AgentLoopRegistrar.unregister("test_branch_other_react")


class _NarrowAgent(BranchFakeAgent):
    """Negotiates differently: no ``loop.action_result``."""

    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["page", "task"], ["step_info", "history"]),
        "action": ("action", ["click", "done"]),
    }

    async def build_react_prompt(self, obs: Any, *, context) -> list[Any]:
        return await super().build_react_prompt(obs, context=context)


class TestCheckpointCompatibility:
    @pytest.mark.parametrize(
        "case",
        [
            "agent_id",
            "knob",
            "numeric_knob",
            "memory",
            "max_actions_per_step",
            "negotiation",
            "source_loop",
            "branch_source",
            "step",
        ],
    )
    async def test_mismatch_raises_before_setup(self, tmp_path: Path, case: str) -> None:
        source_loop: ReactLoop | None = _OtherReact(max_steps=10) if case == "source_loop" else None
        source = await record_source(tmp_path, SOURCE_REPLIES, loop=source_loop)
        kwargs: dict[str, Any] = {}
        step = 2
        if case == "agent_id":
            agent = BranchFakeAgent()
            agent.agent_id = "other_agent"  # type: ignore[misc]
            kwargs.update(agent=agent, config_agent_id="other_agent")
        elif case == "knob":
            kwargs.update(agent=BranchFakeAgent(style="fancy"))
        elif case == "numeric_knob":
            # Recorded raw, and nothing renders it: only this check can see it.
            kwargs.update(agent=BranchFakeAgent(patience=7))
        elif case == "memory":
            kwargs.update(memory="sliding_window")
        elif case == "max_actions_per_step":
            kwargs.update(loop=BranchLoop(max_actions_per_step=2))
        elif case == "negotiation":
            kwargs.update(agent=_NarrowAgent())
        elif case == "branch_source":
            provenance = BranchProvenance(
                source_run_id="src",
                source_trace="src",
                checkpoint_step=1,
                branch_index=0,
                branch_group_id="src/s1",
            )
            first = await run_branch(
                tmp_path, source, 1, ["click 102", "done x"], branch=provenance
            )
            assert first.run_dir is not None
            source = first.run_dir
            step = 1
        elif case == "step":
            step = 9
        cap = make_cap()
        with pytest.raises(IncompatibleCheckpointError) as caught:
            await run_branch(
                tmp_path, source, step, ["click 100"], cap=cap, run_id="refused", **kwargs
            )
        # Refused while assembling: nothing set up, no trace opened.
        assert cap.setups == 0
        assert not (tmp_path / "branches" / "refused").exists()
        message = str(caught.value)
        expected = {
            "agent_id": "agent_id",
            "knob": "agent.style",
            "numeric_knob": "agent.patience",
            "memory": "memory.memory_id",
            "max_actions_per_step": "agent_loop.max_actions_per_step",
            "negotiation": "negotiation.active_fragments",
            "source_loop": "test_branch_other_react",
            "branch_source": "itself a branch run",
            "step": "not committed",
        }[case]
        assert expected in message
        assert caught.value.source in message

    async def test_a_numeric_memory_knob_is_compared(self, tmp_path: Path) -> None:
        source = await record_source(tmp_path, SOURCE_REPLIES, memory="sliding_window", window=3)
        cap = make_cap()
        with pytest.raises(IncompatibleCheckpointError) as caught:
            await run_branch(
                tmp_path, source, 2, ["click 102"], cap=cap, memory="sliding_window", window=5
            )
        assert caught.value.mismatches["memory.window_size"] == (3, 5)
        assert cap.setups == 0

    async def test_the_loops_other_settings_may_differ(self, tmp_path: Path, source: Path) -> None:
        """Only the curated loop settings are compared: ``max_steps`` is the branch's own."""
        out = await run_branch(tmp_path, source, 1, ["done ok"], loop=BranchLoop(max_steps=50))
        assert out.result is not None and out.result.agent_done == "done"

    async def test_the_seed_catches_a_setting_the_source_did_not_record(
        self, tmp_path: Path, source: Path
    ) -> None:
        """A source written before numbers were recorded raw shows ``<omitted>``.

        The assembly check cannot compare that, so the seeded prompt — which
        carries ``max_actions_per_step`` — catches the difference at run time,
        still before any action.
        """
        config_file = source / "config.json"
        config = json.loads(config_file.read_text(encoding="utf-8"))
        config["resolved"]["agent"][0]["agent_loop"]["max_actions_per_step"] = OMITTED
        config_file.write_text(json.dumps(config), encoding="utf-8")
        cap = make_cap()
        with pytest.raises(IncompatibleCheckpointError, match="seeded prompt"):
            await run_branch(
                tmp_path, source, 2, ["click 102"], cap=cap, loop=BranchLoop(max_actions_per_step=2)
            )
        assert cap.log == []

    async def test_replay_mismatch(self, tmp_path: Path, source: Path) -> None:
        """An agent that parses the same reply differently cannot reproduce the source."""

        class _OffByOne(BranchFakeAgent):
            def parse_react_output(self, raw: Any) -> list[Any]:
                actions = super().parse_react_output(raw)
                for action in actions:
                    if "index" in action.params:
                        action.params["index"] += 1
                return actions

        cap = make_cap()
        with pytest.raises(IncompatibleCheckpointError) as caught:
            await run_branch(tmp_path, source, 2, ["click 102"], cap=cap, agent=_OffByOne())
        assert "step 0 actions" in caught.value.mismatches
        assert cap.log == []

    async def test_a_context_mismatch_is_caught_per_call(
        self, tmp_path: Path, source: Path
    ) -> None:
        class _Chatty(BranchFakeAgent):
            async def build_react_prompt(self, obs: Any, *, context) -> list[Any]:
                messages = await super().build_react_prompt(obs, context=context)
                messages[0].content += " (rendered differently)"
                return messages

        cap = make_cap()
        with pytest.raises(IncompatibleCheckpointError, match="step 0 call 0"):
            await run_branch(tmp_path, source, 2, ["click 102"], cap=cap, agent=_Chatty())
        assert cap.log == []

    async def test_missing_checkpoint_input(self, tmp_path: Path) -> None:
        from spectragent.core.runtime import AgentRuntime
        from tests._helpers.assembly import AGENT_ID, make_env, make_run
        from tests._helpers.branch_fakes import CAP_ID, ScriptedLLM, memory_of

        run = make_run(
            agent=BranchFakeAgent(),
            loop=BranchLoop(),
            env=make_env(BranchFakeEnv, {CAP_ID: make_cap()}),
            llm=ScriptedLLM([]),
            memory=memory_of("simple"),
        )
        with pytest.raises(IncompatibleLoopInputError, match="requires checkpoint"):
            AgentRuntime.for_agent(run, AGENT_ID).prepare()

    async def test_a_source_trace_never_closed_is_refused(
        self, tmp_path: Path, source: Path
    ) -> None:
        """The trace's own ledger is a hard condition, however the checkpoint was chosen."""
        manifest = source / "manifest.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["status"] = "writing"
        manifest.write_text(json.dumps(document), encoding="utf-8")
        cap = make_cap()
        with pytest.raises(IncompatibleCheckpointError, match="never closed"):
            await run_branch(tmp_path, source, 1, ["click 100"], cap=cap)
        assert cap.setups == 0

    async def test_a_checkpoint_past_complete_through_is_refused(
        self, tmp_path: Path, source: Path
    ) -> None:
        manifest = source / "manifest.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document.update(
            status="incomplete",
            complete_through=0,
            incomplete_reason="recording failed at step 1: OSError: disk full",
        )
        manifest.write_text(json.dumps(document), encoding="utf-8")
        cap = make_cap()
        with pytest.raises(IncompatibleCheckpointError) as caught:
            await run_branch(tmp_path, source, 1, ["click 100"], cap=cap)
        assert "past the step its trace is complete through (0)" in str(caught.value)
        assert "recording failed at step 1" in str(caught.value)
        assert cap.setups == 0

    async def test_a_source_that_is_not_a_trace(self, tmp_path: Path) -> None:
        empty = tmp_path / "nothing-here"
        empty.mkdir()
        cap = make_cap()
        with pytest.raises(IncompatibleCheckpointError, match="no readable trace"):
            await run_branch(tmp_path, empty, 0, [], cap=cap)
        assert cap.setups == 0


class TestRecordedReplies:
    async def test_parse_retries_and_truncation_replay(self, tmp_path: Path) -> None:
        """A retried step and a truncated reply are rebuilt exactly, then decided again."""
        replies = ["what", "click 100 101", "click 101", "done ok"]
        source = await record_source(tmp_path, replies)
        recorded = RunTrace(source)
        assert len(recorded.llm_messages(0)) == 2  # a re-ask
        assert recorded.step(0).truncated_actions == 1  # type: ignore[union-attr]
        out = await run_branch(tmp_path, source, 2, ["done ok"])
        assert recorded_form(out.llm.prompts[0]) == recorded_form(
            recorded.llm_messages(2)[0].messages
        )

    async def test_a_structured_refusal_replays_as_the_raw_reply(self, tmp_path: Path) -> None:
        from spectragent.core.llm.exceptions import StructuredOutputValidationError

        refusal = StructuredOutputValidationError(
            "reply does not fit", raw_output='{"txt": "click 100"}'
        )
        source = await record_source(
            tmp_path, [refusal, {"text": "click 100"}, "click 101", "done ok"], structured=True
        )
        recorded = RunTrace(source)
        assert [c.status for c in recorded.llm_messages(0)] == ["failed", "completed"]
        out = await run_branch(
            tmp_path, source, 2, ["done ok"], agent=BranchFakeAgent(structured=True)
        )
        assert recorded_form(out.llm.prompts[0]) == recorded_form(
            recorded.llm_messages(2)[0].messages
        )
        assert out.result is not None and out.result.agent_done.value == "done"

    async def test_an_action_the_guard_refused_replays_as_recorded(self, tmp_path: Path) -> None:
        """An ablation dropped ``click``: the guard refused it, and the record kept ``click``.

        The replay names each parsed action as ``guarded_act`` did
        (``resolve_action_name``), so a refused action is compared as the
        agent wrote it — not qualified through a binding whose target the
        ablation removed.
        """
        only_done = [DONE]
        source = await record_source(
            tmp_path, ["click 100", "click 101", "done ok"], action_filter=only_done
        )
        refused = RunTrace(source).step(0)
        assert refused is not None
        assert [a.name for a in refused.actions] == ["click"]
        assert [r.success for r in refused.action_results] == [False]

        out = await run_branch(tmp_path, source, 2, ["done fine"], action_filter=only_done)
        assert out.result is not None and out.result.agent_done.value == "done"
        assert _origins(out.run_dir) == [
            (0, "restored"),
            (1, "restored"),
            (2, "policy"),
            (3, "terminal"),
        ]
        # The restorer re-ran the recorded clicks through the guard, which
        # refused them again — as recorded, so both steps verified.
        assert out.cap.log == [f"{DONE}({{'answer': 'fine'}})"]
        trace = RunTrace(out.run_dir)  # type: ignore[arg-type]
        for step in (0, 1):
            record = trace.step(step)
            assert record is not None and record.restoration is not None
            assert record.restoration.verdict == "ok"

    async def test_the_parse_error_streak_continues_from_the_replay(self, tmp_path: Path) -> None:
        """Step *t-1* was unparseable: one more unparseable policy step ends the branch.

        The source ran ``parse_retries_per_step=1``, so its step 1 is two
        unparseable replies. The branch tolerates two consecutive unparseable
        steps; the replayed step 1 is the first of them, so its policy step is
        the second, and the branch never reaches a second policy step.
        """
        source = await record_source(tmp_path, ["click 100", "what", "huh", "click 101", "done ok"])
        failed = RunTrace(source).step(1)
        assert failed is not None and failed.actions == []
        out = await run_branch(
            tmp_path,
            source,
            2,
            ["nonsense", "done late"],
            loop=BranchLoop(continue_steps=1, max_consecutive_parse_errors=2),
        )
        result = out.result
        assert result is not None
        assert result.agent_done.value == "error"
        assert result.error is not None
        assert result.error.startswith("Parse failed 2 consecutive times")
        assert result.metrics.steps == 1
        assert len(out.llm.prompts) == 1
        assert _origins(out.run_dir) == [
            (0, "restored"),
            (1, "restored"),
            (2, "policy"),
            (3, "terminal"),
        ]

    async def test_a_parsed_step_in_the_replay_resets_the_streak(self, tmp_path: Path) -> None:
        """Step 0 was unparseable, step 1 parsed: the policy step starts a new streak."""
        source = await record_source(tmp_path, ["what", "huh", "click 100", "click 101", "done ok"])
        out = await run_branch(
            tmp_path,
            source,
            2,
            ["nonsense", "done late"],
            loop=BranchLoop(continue_steps=1, max_consecutive_parse_errors=2),
        )
        assert out.result is not None
        assert (out.result.agent_done.value, out.result.answer) == ("done", "late")
        assert out.result.metrics.steps == 2
        assert _origins(out.run_dir) == [
            (0, "restored"),
            (1, "restored"),
            (2, "policy"),
            (3, "policy"),
            (4, "terminal"),
        ]

    async def test_sliding_window_memory(self, tmp_path: Path) -> None:
        source = await record_source(tmp_path, SOURCE_REPLIES, memory="sliding_window", window=3)
        out = await run_branch(tmp_path, source, 3, ["done ok"], memory="sliding_window", window=3)
        assert recorded_form(out.llm.prompts[0]) == recorded_form(
            RunTrace(source).llm_messages(3)[0].messages
        )
        assert out.result is not None and out.result.agent_done.value == "done"
        assert DONE in out.cap.log[-1]


async def test_warn_restoration_does_not_pair_a_later_result_with_a_failed_attempt(
    tmp_path: Path,
) -> None:
    source = await record_source(
        tmp_path,
        ["click 100 101", "done ok"],
        loop=ReactLoop(max_steps=10, max_actions_per_step=2, today_override="2001-02-03"),
    )
    cap = make_cap()
    original = cap.execute_action

    async def fail_first(action):
        if action.params.get("index") == 100:
            raise RuntimeError("first execution failed")
        return await original(action)

    cap.execute_action = fail_first
    loop = BranchLoop(max_actions_per_step=2)
    loop.restorer = PrefixReplayRestorer(verify="warn")
    out = await run_branch(tmp_path, source, 1, ["done ok"], loop=loop, cap=cap)
    assert out.run_dir is not None
    record = RunTrace(out.run_dir).step(0)
    assert record is not None and record.restoration is not None
    assert [action.params["index"] for action in record.actions] == [101]
    assert [result.extracted_content for result in record.action_results] == ["clicked 1"]
    first, second = record.restoration.actions
    assert first.executed is not None and first.executed.params["index"] == 100
    assert first.result is None and first.live_error == "RuntimeError: first execution failed"
    assert second.result == record.action_results[0]
    from spectragent.evaluator.trace_view import TraceView

    history = TraceView(RunTrace(out.run_dir)).restored_action_history
    assert len(history) == 2
    assert "index': 100" in history[0] and "raised: RuntimeError" in history[0]
    assert "index': 101" in history[1]
