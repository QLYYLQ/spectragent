"""A branch sweep: each branch task belongs to its source run's benchmark (§4.10.1).

``docs/architecture_design.md`` §4.10.1 "分叉 sweep 是一种任务来源": a branch
sweep's task source is no benchmark of its own; each branch task carries its
source's ``benchmark_spec`` (``TaskContext.benchmark``), and its run is
composed, set up, judged and recorded as that benchmark's.

Everything is real except the site and the model: sources are recorded
through the real runtime and trace writer on the fake site
(``tests/_helpers/branch_fakes.py``), and branched through the real
``trace_branches`` loader, ``BenchmarkRunner``, ``BranchLoop`` and verifier
pass. The ``world`` benchmark is AndroidWorld in miniature: a setup component
its capability hosts, started on the task the setup context names, a live
verifier judging at the scoring point and an offline verifier judging the
trace after the sweep. The ``shop`` benchmark has only an offline verifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from spectragent.benchmarks import BenchmarkBase, BenchmarkSetupBase
from spectragent.benchmarks.selection import read_benchmark, spec_of
from spectragent.benchmarks.trace_branches import TraceBranches
from spectragent.core.benchmark_runner import (
    BenchmarkRunner,
    PreflightError,
    PrimaryTally,
    TaskBenchmark,
)
from spectragent.core.composer import ComposedRun
from spectragent.core.data_loader.task import TraceBranchesTaskLoader
from spectragent.core.data_loader.task.step_selector import ExplicitSteps
from spectragent.core.loops import BranchLoop
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.benchmark_spec import BenchmarkSpec
from spectragent.schemas.verdict import Judgment
from spectragent.verifiers._registrar import VerifierBase
from tests._helpers.branch_fakes import (
    CAP_ID,
    BranchFakeAgent,
    BranchFakeCap,
    build_run,
    make_cap,
    record_source,
)

#: The fake capability's restoration evidence reads back through the capability registry.
pytestmark = pytest.mark.usefixtures("branch_fake_registered")

#: A three-step source: two clicks, then done; branched at step 1.
SOURCE_REPLIES = ["click 100", "click 101", "done ok"]
CHECKPOINT = 1
#: The setup context key the world's setup component starts its task from.
WORLD_TASK = "world_task"


class _WorldCap(BranchFakeCap):
    """The fake site, hosting a benchmark's setup component and live verifiers.

    Its setup hands the task its context names (``world_task``) to the bound
    setup component, as the Android capability hands AndroidWorld's component
    its task; a live verifier judges the run's answer on the site.
    """

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = CAP_ID

    hosted_setup: Any = None
    hosted_verifiers: tuple[Any, ...] = ()

    def check_setup(
        self,
        context: Any,
        *,
        negotiation: Any = None,
        archivers: Any = (),
        setup: Any = None,
        live_verifiers: Sequence[Any] = (),
    ) -> None:
        return None

    def bind_setup(self, setup: Any) -> None:
        self.hosted_setup = setup

    def bind_live_verifiers(self, verifiers: Sequence[Any]) -> None:
        self.hosted_verifiers = tuple(verifiers)

    async def setup(
        self,
        *,
        negotiation: Any = None,
        start_url: str | None = None,
        replay_archive: str | None = None,
        world_task: str | None = None,
    ) -> None:
        await super().setup(
            negotiation=negotiation, start_url=start_url, replay_archive=replay_archive
        )
        if self.hosted_setup is not None:
            self.hosted_setup.started.append(world_task)

    async def verify_live(self, verifier: Any, *, answer: str | None) -> Judgment:
        return Judgment(label=1 if answer == "ok" else 0, evidence={"clicks": list(self.clicks)})


class _WorldSetup(BenchmarkSetupBase):
    """The world's setup component: it starts each task on the site that hosts it."""

    __abstract__ = True  # Directly built test component; never a YAML implementation.
    __registry_key__ = "world"
    __hosted_by__ = CAP_ID

    def __init__(self) -> None:
        self.started: list[str | None] = []


class _LiveJudge(VerifierBase):
    """The world's live verifier: judged by its host at the scoring point."""

    __abstract__ = True  # Directly built test verifier; never a YAML implementation.
    __registry_key__ = "world_live"
    __needs__ = frozenset({"live_environment", "answer"})
    capability_id = CAP_ID


class _TraceJudge(VerifierBase):
    """An offline verifier: passes a run whose recorded answer is ``ok``."""

    __abstract__ = True  # Directly built test verifier; never a YAML implementation.
    __needs__ = frozenset({"trace", "answer"})

    async def verify(self, trace: RunTrace, reference: Any) -> Judgment:
        result = trace.result
        return Judgment(label=1 if result is not None and result.answer == "ok" else 0)


class _WorldTraceJudge(_TraceJudge):
    __abstract__ = True
    __registry_key__ = "world_trace"


class _ShopTraceJudge(_TraceJudge):
    __abstract__ = True
    __registry_key__ = "shop_trace"


class _World(BenchmarkBase):
    __abstract__ = True  # Directly built test benchmark; never a YAML implementation.
    __registry_key__ = "world"
    __primary_verifier__ = "world_live"

    setup: _WorldSetup  # type: ignore[assignment]


class _Shop(BenchmarkBase):
    __abstract__ = True  # Directly built test benchmark; never a YAML implementation.
    __registry_key__ = "shop"
    __primary_verifier__ = "shop_trace"


def _world() -> _World:
    benchmark = _World()
    benchmark.setup = _WorldSetup()
    benchmark.verifiers = [_LiveJudge(), _WorldTraceJudge()]
    return benchmark


def _shop() -> _Shop:
    benchmark = _Shop()
    benchmark.verifiers = [_ShopTraceJudge()]
    return benchmark


def _spec(tmp_path: Path, benchmark_id: str) -> BenchmarkSpec:
    """The record of selecting *benchmark_id* by the path of its YAML, which a branch reads again."""
    path = tmp_path / f"{benchmark_id}.yaml"
    path.write_text(f"benchmark:\n  - benchmark_id: {benchmark_id}\n    task_loader: {{}}\n")
    return spec_of(str(path), read_benchmark(str(path)))


async def _source(
    tmp_path: Path, run_id: str, benchmark: Any = None, spec: BenchmarkSpec | None = None
) -> Path:
    """A recorded run of *benchmark* on the site, selected as *spec* says."""
    return await record_source(
        tmp_path,
        SOURCE_REPLIES,
        run_id=run_id,
        cap=make_cap(cls=_WorldCap),
        env_context={WORLD_TASK: f"{run_id}-task"},
        benchmark=benchmark,
        benchmark_spec=spec,
    )


@dataclass
class _Sweep:
    """A branch sweep over ``tmp_path/sources``: its loader, and every stack it composed, by benchmark."""

    tmp_path: Path
    benchmarks: dict[str, Any]
    stacks: dict[str | None, list[ComposedRun]] = field(default_factory=dict)
    inherited: list[BenchmarkSpec] = field(default_factory=list)
    refuse: frozenset[str] = frozenset()
    loader: TraceBranchesTaskLoader = field(init=False)

    def __post_init__(self) -> None:
        self.loader = TraceBranchesTaskLoader(
            data_dir=str(self.tmp_path / "sources"), require_archive=()
        )
        self.loader.step_selector = ExplicitSteps(steps=[CHECKPOINT])

    def _stack(self, benchmark_id: str | None) -> ComposedRun:
        """A fresh stack: the task source, and the benchmark *benchmark_id* when the task has one."""
        run, _writer, _llm = build_run(
            self.tmp_path / "branches",
            loop=BranchLoop(),
            replies=["done ok"],
            agent=BranchFakeAgent(),
            cap=make_cap(cls=_WorldCap),
        )
        task_source = TraceBranches()
        task_source.task_loader = self.loader
        blocks: dict[str, Any] = {"trace_branches#0": task_source}
        if benchmark_id is not None:
            blocks[f"{benchmark_id}#0"] = self.benchmarks[benchmark_id]()
        run.by_layer["benchmark"] = blocks
        self.stacks.setdefault(benchmark_id, []).append(run)
        return run

    def task_benchmark(self, spec: BenchmarkSpec) -> TaskBenchmark:
        """What ``spectragent bench`` builds from a source's record (``inherited_benchmark``)."""
        self.inherited.append(spec)
        if spec.benchmark_id in self.refuse:
            raise ValueError(f"the benchmark {spec.benchmark_id!r} does not compose here")
        judge = self.benchmarks[spec.benchmark_id]()
        return TaskBenchmark(
            compose=lambda: self._stack(spec.benchmark_id),
            verifiers=judge.offline_verifiers(),
            primary_rule=judge,
        )

    def runner(self) -> BenchmarkRunner:
        return BenchmarkRunner(
            loader=self.loader,
            compose=lambda: self._stack(None),
            timeout=30,
            benchmark_spec=_spec(self.tmp_path, "trace_branches"),
            inherited=self.task_benchmark,
        )


def _verdicts(trace_dir: Path | None) -> dict[str, Any]:
    assert trace_dir is not None
    return {
        verdict.verifier.verifier_id: verdict for verdict in RunTrace(trace_dir).verdicts().values()
    }


class TestAnAndroidWorldStyleSource:
    """Its branch hosts the source benchmark's setup component and is judged by its verifiers."""

    async def test_the_branch_is_its_source_benchmarks_run(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, "world")
        await _source(tmp_path, "src-world", _world(), spec)
        sweep = _Sweep(tmp_path, {"world": _world})

        summary = await sweep.runner().run()

        [outcome] = summary.outcomes
        assert outcome.task_id == "src-world/s1/b0" and outcome.ok
        assert outcome.result.agent_done == "done"
        # Composed once, by the pre-flight, as the world benchmark's run.
        assert sweep.inherited == [spec] and list(sweep.stacks) == ["world"]
        [stack] = sweep.stacks["world"]
        assert stack.instance("benchmark", "world#0").setup.started == ["src-world-task"]
        # Its trace says which benchmark it belongs to: its source's.
        trace = RunTrace(outcome.trace_dir)  # type: ignore[arg-type]
        assert trace.config.benchmark_spec == spec
        assert trace.config.benchmark == "trace_branches", "the data's join key is unchanged"
        assert trace.config.branch is not None and trace.config.branch.source_run_id == "src-world"
        # Judged live at the scoring point, and offline after the sweep.
        verdicts = _verdicts(outcome.trace_dir)
        assert set(verdicts) == {"world_live", "world_trace"}
        assert (verdicts["world_live"].attachment, verdicts["world_live"].label) == ("live", 1)
        assert verdicts["world_live"].evidence == {"clicks": [0]}, "the restored prefix's click"
        assert (verdicts["world_trace"].attachment, verdicts["world_trace"].label) == (
            "offline",
            1,
        )
        [tally] = summary.verification
        assert (tally.verifier.verifier_id, tally.judged, tally.passed) == ("world_trace", 1, 1)
        assert summary.primaries == [
            PrimaryTally(verifier_id="world_live", tasks=1, judged=1, success_rate=1.0)
        ]
        assert outcome.primary_verifier == "world_live" and outcome.success == 1.0

    async def test_the_source_ran_as_its_branch_does(self, tmp_path: Path) -> None:
        """The source hosted the same component and recorded how its benchmark was selected."""
        spec = _spec(tmp_path, "world")
        world = _world()
        source = await _source(tmp_path, "src-world", world, spec)

        assert world.setup.started == ["src-world-task"]
        assert RunTrace(source).config.benchmark_spec == spec
        assert set(_verdicts(source)) == {"world_live"}, "no offline pass ran on the source"

    async def test_without_eval_only_the_live_verdict_is_made(self, tmp_path: Path) -> None:
        """``--no-eval`` skips the offline pass, never the scoring point."""
        await _source(tmp_path, "src-world", _world(), _spec(tmp_path, "world"))
        sweep = _Sweep(tmp_path, {"world": _world})
        task_benchmark = sweep.task_benchmark

        def unjudged(spec: BenchmarkSpec) -> TaskBenchmark:
            built = task_benchmark(spec)
            return TaskBenchmark(compose=built.compose, primary_rule=built.primary_rule)

        sweep.task_benchmark = unjudged  # type: ignore[method-assign]
        summary = await sweep.runner().run()

        assert set(_verdicts(summary.outcomes[0].trace_dir)) == {"world_live"}
        assert summary.verification == []


class TestAMixedSweep:
    """Sources of several benchmarks: each task is composed, recorded and judged as its own's."""

    async def _record(self, tmp_path: Path) -> dict[str, BenchmarkSpec]:
        specs = {"world": _spec(tmp_path, "world"), "shop": _spec(tmp_path, "shop")}
        await _source(tmp_path, "a-world", _world(), specs["world"])
        await _source(tmp_path, "b-shop", _shop(), specs["shop"])
        await _source(tmp_path, "c-plain")
        return specs

    async def test_each_task_belongs_to_its_sources_benchmark(self, tmp_path: Path) -> None:
        specs = await self._record(tmp_path)
        sweep = _Sweep(tmp_path, {"world": _world, "shop": _shop})

        summary = await sweep.runner().run()

        by_task = {outcome.task_id: outcome for outcome in summary.outcomes}
        assert list(by_task) == ["a-world/s1/b0", "b-shop/s1/b0", "c-plain/s1/b0"]
        assert all(outcome.ok for outcome in summary.outcomes)
        recorded = {
            task_id: RunTrace(outcome.trace_dir).config.benchmark_spec  # type: ignore[arg-type]
            for task_id, outcome in by_task.items()
        }
        assert recorded == {
            "a-world/s1/b0": specs["world"],
            "b-shop/s1/b0": specs["shop"],
            "c-plain/s1/b0": None,
        }, "a branch of a run of no benchmark belongs to none, never to the task source"
        # One stack per task, each its own benchmark's; only the world's has a setup.
        assert {key: len(stacks) for key, stacks in sweep.stacks.items()} == {
            "world": 1,
            "shop": 1,
            None: 1,
        }
        [world] = sweep.stacks["world"]
        assert world.instance("benchmark", "world#0").setup.started == ["a-world-task"]
        assert {key: set(_verdicts(o.trace_dir)) for key, o in by_task.items()} == {
            "a-world/s1/b0": {"world_live", "world_trace"},
            "b-shop/s1/b0": {"shop_trace"},
            "c-plain/s1/b0": set(),
        }
        assert sorted(t.verifier.verifier_id for t in summary.verification) == [
            "shop_trace",
            "world_trace",
        ]
        # A success rate per primary kind: across judges it would mean nothing.
        assert summary.primaries == [
            PrimaryTally(verifier_id="world_live", tasks=1, judged=1, success_rate=1.0),
            PrimaryTally(verifier_id="shop_trace", tasks=1, judged=1, success_rate=1.0),
        ]
        table = summary.format_table()
        assert "primary verdict (world_live): success rate 100.0% over the 1 of 1" in table
        assert "primary verdict (shop_trace): success rate 100.0% over the 1 of 1" in table
        assert [entry["verifier_id"] for entry in summary.as_dict()["primary"]] == [
            "world_live",
            "shop_trace",
        ]

    async def test_each_benchmark_is_checked_before_any_task_runs(self, tmp_path: Path) -> None:
        """One pre-flight per benchmark: the shop's refusal stops the sweep before the world's task."""
        await self._record(tmp_path)
        sweep = _Sweep(tmp_path, {"world": _world, "shop": _shop}, refuse=frozenset({"shop"}))

        with pytest.raises(PreflightError) as info:
            await sweep.runner().run()

        assert info.value.task_id == "b-shop/s1/b0"
        assert "the benchmark 'shop' does not compose here" in str(info.value)
        assert not (tmp_path / "branches").exists(), "no task ran, no trace was opened"

    async def test_a_sweep_that_cannot_compose_another_benchmark_fails_only_its_tasks(
        self, tmp_path: Path
    ) -> None:
        await _source(tmp_path, "a-world", _world(), _spec(tmp_path, "world"))
        await _source(tmp_path, "c-plain")
        sweep = _Sweep(tmp_path, {"world": _world})
        runner = BenchmarkRunner(
            loader=sweep.loader, compose=lambda: sweep._stack(None), timeout=30
        )

        outcome = await runner.run_task("a-world/s1/b0")

        assert outcome.error is not None and "no way to compose a task of another benchmark" in (
            outcome.error
        )
        assert (await runner.run_task("c-plain/s1/b0")).ok
