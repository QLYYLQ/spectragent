"""A branch of an AndroidWorld run is an AndroidWorld run (``docs/architecture_design.md`` §4.10.1).

"分叉 sweep 是一种任务来源": the branch sweep's task source is no benchmark of
its own, and each branch task belongs to its source run's benchmark, found
again by how the source selected it (``TraceConfig.benchmark_spec``). Through
the real ``ConfigComposer``, the ``trace_branches`` loader and one
``AgentRuntime.prepare()``, with a fake AndroidWorld installation and nothing
launched:

- the branch run configuration writes no AVD and no app table, as the
  source's did not: AndroidWorld's part is merged into it again;
- AndroidWorld's setup component and live verifier are bound to the Android
  capability, as for the source;
- ``bench``, ``run --task-id`` and ``config check --task-id`` all compose the
  task's run root from the task source and the source's benchmark block; the
  task source alone gives the tasks, since the run configuration does not
  compose without AndroidWorld's part.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import JsonValue

from spectragent.benchmarks.androidworld.benchmark import AndroidWorldBenchmark
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.benchmarks.androidworld.verifier import AndroidWorldVerifier
from spectragent.benchmarks.selection import run_root, spec_of
from spectragent.core.archive.privacy import RedactionLedger
from spectragent.core.benchmark_runner import (
    BenchmarkRunner,
    TaskOutcome,
    compose_benchmark_alone,
    inherited_benchmark,
)
from spectragent.core.capabilities.android.actions import AndroidOpenAppAction
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.composer import ComposedRun, ComposeError, ConfigComposer
from spectragent.core.data_loader.task import TraceBranchesTaskLoader
from spectragent.core.runtime import AgentRuntime
from spectragent.core.trace.models import (
    TaskSetupRecord,
    TraceConfig,
    TraceNegotiation,
    TraceStatus,
)
from spectragent.core.trace.redaction import redact_config
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.step_record import StepRecord
from tests._helpers.android_device import fake_service_apk
from tests._helpers.benchmark_data import ANDROIDWORLD_APPS, androidworld_data, androidworld_task
from tests._helpers.fake_androidworld import fake_installation
from tests._helpers.trace_builder import agent_result

TASK = "aw-source/s1/b0"


def _configuration(tmp_path: Path, loop: dict[str, JsonValue]) -> dict[str, Any]:
    """UI-TARS on Android, writing no AVD and no app table: the benchmark gives both."""
    return {
        "agent": [
            {
                "agent_id": "ui_tars",
                "coordinate_space": "screenshot_pixels",
                "agent_loop": {**loop, "max_steps": 10},
                "llm": {"model_type": "openai", "model": "m", "provider": {"name": "direct"}},
                "memory": {"memory_id": "simple"},
            }
        ],
        "environment": [
            {
                "env_type": "android",
                "capabilities": [
                    {
                        "capability_id": "android",
                        "service_apk": str(fake_service_apk(tmp_path)),
                        "tap_injector": {"injector_id": "shell_injection"},
                        "typing_backend": {"typing_id": "set_text"},
                        "ui_tree_cleaner": {"cleaner_id": "androidworld"},
                        "ui_tree_serializer": {"serializer_id": "m3a"},
                    }
                ],
            }
        ],
        "hook": [{"hook_id": "trace_writer", "trace_dir": str(tmp_path / "traces")}],
    }


class _Sweep:
    """An AndroidWorld run recorded as a source, and a branch sweep over it."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The emulator's gRPC token was measured on macOS only (``local_emulator``).
        monkeypatch.setattr(sys, "platform", "darwin")
        data = androidworld_data(
            tmp_path / "data", [androidworld_task(params={"max_or_min": "max"})]
        )
        block = {
            "benchmark_id": "androidworld",
            "task_loader": {"benchmark_name": "androidworld", "data_dir": str(data)},
            "setup": {
                "setup_id": "androidworld",
                "root": str(fake_installation(tmp_path / "aw", monkeypatch)),
            },
            "verifiers": [{"verifier_id": "androidworld"}],
            "capability": [
                {
                    "capability_id": "android",
                    "backend": {"provider_id": "local_emulator", "avd": "AndroidWorldAvd"},
                }
            ],
        }
        self.benchmark = tmp_path / "my_androidworld.yaml"
        self.benchmark.write_text(yaml.safe_dump({"benchmark": [block]}))
        self.spec = spec_of(str(self.benchmark), block)
        self.sources = tmp_path / "sources"
        self._record(_configuration(tmp_path, {"loop_id": "react"}), block)

        self.configuration = _configuration(
            tmp_path, {"loop_id": "branch", "restorer": {"restorer_id": "prefix_replay"}}
        )
        self.config_path = tmp_path / "branch.yaml"
        self.config_path.write_text(yaml.safe_dump(self.configuration))
        self.task_source: dict[str, Any] = {
            "benchmark_id": "trace_branches",
            "task_loader": {
                "benchmark_name": "trace_branches",
                "data_dir": str(self.sources),
                "require_archive": [],
                "step_selector": {"selector_id": "explicit", "steps": [1]},
            },
        }
        self.branches = tmp_path / "branches.yaml"
        self.branches.write_text(yaml.safe_dump({"benchmark": [self.task_source]}))

    def _record(self, configuration: dict[str, Any], block: dict[str, Any]) -> None:
        """The source: composed and prepared as ``bench --benchmark <the YAML>`` runs it, two steps."""
        root = run_root(configuration, block)
        run = ConfigComposer.for_config(root).compose(root)
        benchmark = run.single("benchmark")
        [task_id] = benchmark.task_loader.task_ids()
        context = benchmark.task_loader.get_task_context(task_id)
        runtime = AgentRuntime.for_agent(run, run.ids("agent")[0])
        runtime.prepare(task_id=task_id, env_context=context.env, loop_inputs=context.loop)
        wal = WALWriter(self.sources, "aw-source")
        wal.init_run(
            TraceConfig(
                agent_id="ui_tars",
                loop_id="react",
                env_type="android",
                llm_model="m",
                benchmark="androidworld",
                subset="All",
                task="Turn brightness to the max value.",
                task_id=task_id,
                resolved=redact_config(run.run_configuration()),
                action_space=runtime.action_space,
                negotiation=TraceNegotiation.from_results(
                    runtime.negotiated_observation,
                    runtime.negotiated_actions,
                    runtime.negotiated_archive,
                ),
                task_setup=TaskSetupRecord(context=context.env, redactions=RedactionLedger()),
                max_steps=10,
                benchmark_spec=self.spec,
            )
        )
        for step in range(2):
            wal.commit_step(
                step,
                StepRecord(
                    step=step,
                    observation_summary="the home screen",
                    llm_output="wait",
                    actions=[Action(name="android.wait", params={})],
                    action_results=[ActionResult(success=True)],
                ),
            )
        wal.write_result(agent_result())
        wal.finalize(TraceStatus.COMPLETE)


@pytest.fixture
def sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Sweep:
    return _Sweep(tmp_path, monkeypatch)


def _is_androidworlds(run: ComposedRun) -> None:
    """*run* is composed and bound as an AndroidWorld run: its part merged, its components hosted."""
    capability = run.single("capability")
    assert isinstance(capability, AndroidCapability)
    assert capability.backend.avd == "AndroidWorldAvd"  # type: ignore[attr-defined]
    (open_app,) = [a for a in capability.actions if isinstance(a, AndroidOpenAppAction)]
    assert open_app.apps == ANDROIDWORLD_APPS
    assert isinstance(capability._setup, AndroidWorldSetup), "bound by the pre-flight"
    (verifier,) = capability._live_verifiers
    assert isinstance(verifier, AndroidWorldVerifier)
    recorded = run.run_configuration()
    assert recorded is not None
    blocks = recorded.model_dump()["benchmark"]
    assert [block["benchmark_id"] for block in blocks] == ["trace_branches", "androidworld"]
    assert all(block["verifiers"] is None for block in blocks if "verifiers" in block)


def test_the_branch_task_names_its_sources_benchmark(sweep: _Sweep) -> None:
    loader = compose_benchmark_alone(sweep.task_source).task_loader
    assert loader.task_ids() == [TASK]
    context = loader.get_task_context(TASK)
    assert context.benchmark == sweep.spec
    assert context.env == {"oracle_task": context.env["oracle_task"]}, "no replay archive"


def test_the_pre_flight_composes_and_binds_it_as_an_androidworld_run(sweep: _Sweep) -> None:
    loader = compose_benchmark_alone(sweep.task_source).task_loader
    own = run_root(sweep.configuration, sweep.task_source)
    built = inherited_benchmark(sweep.configuration, sweep.task_source, sweep.spec)
    runner = BenchmarkRunner(
        loader=loader,
        compose=lambda: ConfigComposer.for_config(own).compose(own),
        inherited=lambda spec: built,
    )

    _is_androidworlds(runner.preflight(TASK))
    assert isinstance(built.primary_rule, AndroidWorldBenchmark)
    assert built.verifiers == [], "its one verifier judges live, at the scoring point"


def test_the_run_configuration_alone_does_not_compose(sweep: _Sweep) -> None:
    """Why the tasks come from the task source's block alone: the benchmark writes the AVD."""
    own = run_root(copy.deepcopy(sweep.configuration), sweep.task_source)
    with pytest.raises(ComposeError, match="backend: required, and neither"):
        ConfigComposer.for_config(own).compose(own)


def test_bench_runs_it_on_its_benchmarks_stack(
    sweep: _Sweep, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from spectragent.core.benchmark_runner import main

    handed: list[ComposedRun | None] = []

    async def run_task(self: BenchmarkRunner, task_id: str, *, run: Any = None) -> TaskOutcome:
        handed.append(run)
        return TaskOutcome(task_id=task_id)

    monkeypatch.setattr(BenchmarkRunner, "run_task", run_task)
    argv = ["--config", str(sweep.config_path), "--benchmark", str(sweep.branches)]

    assert main([*argv, "--list"]) == 0
    assert capsys.readouterr().out.split() == [TASK]
    assert main([*argv, "--session-dir", str(tmp_path / ".sessions")]) == 0

    [stack] = handed
    assert stack is not None, "the task runs on the stack its pre-flight checked"
    _is_androidworlds(stack)


def test_bench_refuses_up_front_a_source_benchmark_that_does_not_compose(
    sweep: _Sweep, tmp_path: Path, capsys: Any
) -> None:
    """Its YAML is found, so the source is branchable; its data is gone, so its block never composes."""
    from spectragent.core.benchmark_runner import PREFLIGHT_EXIT_CODE, main

    (tmp_path / "data" / "All.json").unlink()
    argv = ["--config", str(sweep.config_path), "--benchmark", str(sweep.branches)]

    assert main([*argv, "--session-dir", str(tmp_path / ".sessions")]) == PREFLIGHT_EXIT_CODE
    error = capsys.readouterr().err
    assert f"refused the configuration for task {TASK!r}, so no task was run" in error
    assert not (tmp_path / "traces").exists() and not (tmp_path / ".sessions").exists()


def test_a_sweep_with_no_branch_left_composes_nothing(
    sweep: _Sweep, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Its only source was left out: no stack is composed, and the run configuration needs one."""
    from spectragent.core.benchmark_runner import main

    sweep.benchmark.unlink()
    sweep.task_source["task_loader"]["on_unbranchable"] = "skip"
    sweep.branches.write_text(yaml.safe_dump({"benchmark": [sweep.task_source]}))
    monkeypatch.chdir(tmp_path)
    argv = ["--config", str(sweep.config_path), "--benchmark", str(sweep.branches)]

    assert main([*argv, "--list"]) == 0
    assert capsys.readouterr().out.split() == []
    assert main(argv) == 0
    assert "tasks=0" in capsys.readouterr().out
    assert len(list((tmp_path / "traces" / ".sessions").iterdir())) == 1, "the default root"


def test_run_and_config_check_compose_the_same_root(sweep: _Sweep) -> None:
    from spectragent.cli.configuration import explain_config
    from spectragent.core import run_cli

    argv = ["--config", str(sweep.config_path), "--benchmark", str(sweep.branches)]
    assert run_cli.main([*argv, "--task-id", TASK, "--dry-run"]) == 0
    checked = explain_config(sweep.config_path, benchmark=str(sweep.branches), task_id=TASK)
    assert checked["checked"] == "configuration_and_prepare"
    blocks = checked["resolved"]["benchmark"]
    assert [block["benchmark_id"] for block in blocks] == ["trace_branches", "androidworld"]


def test_a_source_whose_benchmark_yaml_is_gone_is_unbranchable(sweep: _Sweep) -> None:
    sweep.benchmark.unlink()
    loader = compose_benchmark_alone(
        {
            **sweep.task_source,
            "task_loader": {**sweep.task_source["task_loader"], "on_unbranchable": "skip"},
        }
    ).task_loader
    assert isinstance(loader, TraceBranchesTaskLoader)
    assert loader.task_ids() == []
    assert "is no longer found by how the run selected it" in loader.skipped["aw-source"]
