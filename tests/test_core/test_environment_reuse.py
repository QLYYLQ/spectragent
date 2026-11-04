"""Reusing an environment across a sweep's tasks: the identity, the setup's reset, the count.

The user's ruling of 2026-11-03 (``docs/architecture_design.md`` §4.10.1
"环境跨任务复用"): a sweep sets a task up on the environment the task before it
used only when its identity is the same and the benchmark's setup component
declares that it resets the state between tasks; otherwise every task gets a
new one, the default. Each trace records how many tasks its environment served
before it. A fake capability here "boots" a device per new environment and
logs every boot and close, so each test reads what the sweep did with them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from spectragent.core import benchmark_runner
from spectragent.core.benchmark_runner import BenchmarkRunner, BenchmarkRunSummary
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.capabilities.base import ReusableEnvironment
from spectragent.core.environment_reuse import EnvironmentHandover, EnvironmentReuse
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.experiment import ExperimentScope
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.task import TaskContext
from tests._helpers.assembly import make_env, make_run

CAP = "reuse_cap"
DONE = f"{CAP}.done"


@dataclass
class _Device:
    """What the fake capability brings up; it logs its own close."""

    number: int
    log: list[str]
    #: How long it takes to shut down.
    close_s: float = 0.0

    async def close(self) -> None:
        await asyncio.sleep(self.close_s)
        self.log.append(f"device {self.number} closed")


@dataclass
class _Sweep:
    """What every task's capability shares: the devices booted so far and one log."""

    devices: list[_Device] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    #: Each task's device, in task order.
    ran_on: list[int] = field(default_factory=list)
    #: How long each device takes to shut down.
    close_s: float = 0.0


class _DeviceCap(CapabilityBase):
    """Boots a device per new environment, takes a kept one from the handover, hands it over when
    told to keep it, unless its teardown fails."""

    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = CAP
    __wiring__ = {"action": ("action", [DONE])}

    def __init__(
        self, *, sweep: _Sweep, image: str = "image-a", unclean: bool = False, **kw: Any
    ) -> None:
        super().__init__(**kw)
        self.sweep = sweep
        self.image = image
        self.unclean = unclean
        self._handover: EnvironmentHandover | None = None
        self._device: _Device | None = None

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name=DONE, description="Done")]

    async def execute_action(self, action: Action) -> ActionResult:
        return ActionResult(success=True, is_done=True, extracted_content="answer")

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Any = (),
        setup: Any = None,
    ) -> None:
        if context.get("refuse"):
            raise ValueError("this task cannot run here")

    def bind_setup(self, setup: Any) -> None:
        return None

    def environment_identity(self, setup: Any) -> str:
        return self.image

    def bind_handover(self, handover: EnvironmentHandover | None) -> None:
        self._handover = handover

    async def setup(self, *, negotiation: Any = None, refuse: bool = False) -> None:
        await super().setup(negotiation=negotiation)
        kept = self._handover.take(self.capability_id) if self._handover is not None else None
        if kept is None:
            device = _Device(len(self.sweep.devices) + 1, self.sweep.log, self.sweep.close_s)
            self.sweep.devices.append(device)
            self.sweep.log.append(f"device {device.number} booted")
        else:
            assert isinstance(kept, _Device)
            device = kept
        self._device = device
        self.sweep.ran_on.append(device.number)

    async def teardown(self) -> None:
        device, self._device = self._device, None
        if device is None:
            return
        if self._handover is not None and self._handover.keep and not self.unclean:
            self._handover.hand_over(self.capability_id, device)
        else:
            await device.close()


class _Env(EnvironmentBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "reuse_env"
    __wiring__ = {"capabilities": "capability"}


@dataclass
class _Setup:
    """The benchmark's setup component, hosted by the device capability."""

    resets_between_tasks: bool = True
    setup_id: str = "resetting"
    capability_id: str = CAP


@dataclass
class _Benchmark:
    setup: _Setup | None = None

    def live_verifiers(self) -> list[Any]:
        return []


class _Agent:
    __execution_limit_fields__ = frozenset()
    __wiring__ = {"action": ("action", [DONE])}

    async def build_react_prompt(self, obs: Any, *, context: Any) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Action]:
        return [Action(name=DONE)]

    def get_react_output_model(self) -> None:
        return None


class _Completion:
    completion = "done"
    usage = None


class _LLM:
    inference_kind = "chat"
    model = "test-model"
    name = "test-model"

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()

    async def ainvoke(self, messages: Any, **kw: Any) -> _Completion:
        return _Completion()


class _HangingLLM(_LLM):
    """Never answers: its task runs until the sweep is cancelled."""

    async def ainvoke(self, messages: Any, **kw: Any) -> _Completion:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

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


@dataclass
class _Task:
    task_id: str
    task_text: str = "do it"


class _Loader:
    benchmark_name = "reuse_bench"
    subset = ""

    def __init__(self, count: int = 3, refused: frozenset[str] = frozenset()) -> None:
        self._ids = [f"t-{i}" for i in range(1, count + 1)]
        self._refused = refused

    def task_ids(self) -> list[str]:
        return list(self._ids)

    def __getitem__(self, task_id: str) -> _Task:
        return _Task(task_id)

    def get_task_context(self, task_id: str) -> TaskContext:
        return TaskContext(env={"refuse": True} if task_id in self._refused else {})


def _runner(
    tmp_path: Path,
    sweep: _Sweep,
    *,
    setup: _Setup | None = None,
    images: list[str] | None = None,
    unclean: frozenset[int] = frozenset(),
    hangs: frozenset[int] = frozenset(),
    loader: _Loader | None = None,
    reuse: bool = True,
) -> BenchmarkRunner:
    """A sweep composing a fresh stack per task, as ``spectragent bench`` does, asking for reuse
    unless *reuse* is false (``--reuse-environment``).

    Task *n* (from 1) gets ``images[n - 1]`` as its environment identity, its
    capability's teardown fails when *n* is in *unclean*, and its model never
    answers when *n* is in *hangs*.
    """
    composed = 0

    def compose() -> Any:
        nonlocal composed
        composed += 1
        image = images[composed - 1] if images else "image-a"
        cap = _DeviceCap(sweep=sweep, image=image, unclean=composed in unclean)
        return make_run(
            agent=_Agent(),
            loop=ReactLoop(max_steps=2),
            env=make_env(_Env, {CAP: cap}),
            llm=_HangingLLM() if composed in hangs else _LLM(),
            memory=_Memory(),
            hooks=[TraceWriter(trace_dir=str(tmp_path / "traces"))],
            benchmark=_Benchmark(setup=setup),
        )

    return BenchmarkRunner(loader=loader or _Loader(), compose=compose, reuse_environment=reuse)


def _earlier_tasks(summary: BenchmarkRunSummary) -> list[int]:
    """What each trace the sweep wrote records as its environment's earlier tasks."""
    return [
        RunTrace(outcome.trace_dir).config.earlier_tasks_on_environment
        for outcome in summary.outcomes
        if outcome.trace_dir is not None
    ]


def test_the_device_capability_is_one_whose_environment_can_be_reused() -> None:
    assert isinstance(_DeviceCap(sweep=_Sweep()), ReusableEnvironment)


async def test_a_setup_that_resets_runs_every_task_on_one_device_and_each_trace_counts(
    tmp_path: Path,
) -> None:
    sweep = _Sweep()
    summary = await _runner(tmp_path, sweep, setup=_Setup()).run()

    assert summary.completed == 3
    assert sweep.ran_on == [1, 1, 1]
    assert sweep.log == ["device 1 booted", "device 1 closed"], "closed once, when the sweep ended"
    assert _earlier_tasks(summary) == [0, 1, 2]
    hashes = {
        RunTrace(outcome.trace_dir).manifest.config_hash
        for outcome in summary.outcomes
        if outcome.trace_dir is not None
    }
    assert len(hashes) == 1, "the count is per task, never part of the configuration's identity"


async def test_a_device_slower_to_shut_down_than_the_scopes_cleanup_limit_ends_the_sweep_well(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep closes the kept device after its last task, as a task's teardown would, not
    under the experiment scope's cleanup limit: an emulator may take longer to exit."""
    monkeypatch.setattr(
        benchmark_runner, "ExperimentScope", lambda: ExperimentScope(cleanup_timeout_s=0.05)
    )
    sweep = _Sweep(close_s=0.3)
    summary = await _runner(tmp_path, sweep, setup=_Setup()).run()

    assert summary.completed == 3
    assert sweep.log == ["device 1 booted", "device 1 closed"]


async def test_a_cancelled_sweep_closes_the_device_a_finished_task_kept(tmp_path: Path) -> None:
    sweep = _Sweep()
    running = asyncio.create_task(
        _runner(tmp_path, sweep, setup=_Setup(), hangs=frozenset({2})).run()
    )
    for _ in range(500):  # until task 2 is set up on the kept device
        if len(sweep.ran_on) == 2:
            break
        await asyncio.sleep(0.01)
    assert sweep.ran_on == [1, 1]

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert sweep.log == ["device 1 booted", "device 1 closed"]


async def test_a_fresh_environment_writes_no_count_into_the_trace(tmp_path: Path) -> None:
    sweep = _Sweep()
    summary = await _runner(tmp_path, sweep, setup=_Setup()).run(limit=1)

    written = (Path(summary.outcomes[0].trace_dir or "") / "config.json").read_text()
    assert "earlier_tasks_on_environment" not in written


@pytest.mark.parametrize(
    ("reuse", "setup", "why"),
    [
        (False, _Setup(), None),
        (True, None, "its benchmark setup declares no reset between tasks"),
        (True, _Setup(resets_between_tasks=False), "its benchmark setup declares no reset"),
    ],
    ids=["not asked for", "asked, no setup", "asked, a setup that resets nothing"],
)
async def test_every_task_gets_a_new_device_unless_reuse_is_asked_for_and_can_be_had(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    reuse: bool,
    setup: _Setup | None,
    why: str | None,
) -> None:
    """The default is a new device per task (the user's ruling of 2026-11-04). A sweep that asks for
    reuse it cannot have runs fresh and says why, once."""
    sweep = _Sweep()
    summary = await _runner(tmp_path, sweep, setup=setup, reuse=reuse).run()

    assert sweep.ran_on == [1, 2, 3]
    assert sweep.log == [
        "device 1 booted",
        "device 1 closed",
        "device 2 booted",
        "device 2 closed",
        "device 3 booted",
        "device 3 closed",
    ]
    assert _earlier_tasks(summary) == [0, 0, 0]
    said = [
        record.getMessage() for record in caplog.records if "was asked for" in record.getMessage()
    ]
    if why is None:
        assert said == []
    else:
        assert len(said) == 1 and why in said[0]


async def test_another_identity_gets_a_new_device_and_the_kept_one_is_closed_first(
    tmp_path: Path,
) -> None:
    sweep = _Sweep()
    summary = await _runner(
        tmp_path, sweep, setup=_Setup(), images=["image-a", "image-b", "image-b"]
    ).run()

    assert sweep.ran_on == [1, 2, 2]
    assert sweep.log == [
        "device 1 booted",
        "device 1 closed",
        "device 2 booted",
        "device 2 closed",
    ]
    assert _earlier_tasks(summary) == [0, 0, 1]


async def test_a_task_that_ended_uncleanly_hands_nothing_on_and_the_count_starts_again(
    tmp_path: Path,
) -> None:
    sweep = _Sweep()
    summary = await _runner(tmp_path, sweep, setup=_Setup(), unclean=frozenset({2})).run()

    assert sweep.ran_on == [1, 1, 2]
    assert sweep.log == [
        "device 1 booted",
        "device 1 closed",
        "device 2 booted",
        "device 2 closed",
    ]
    assert _earlier_tasks(summary) == [0, 1, 0]


async def test_a_task_refused_before_setup_leaves_the_kept_device_for_the_next(
    tmp_path: Path,
) -> None:
    """The kept device was never taken, so it is as the task before left it."""
    sweep = _Sweep()
    loader = _Loader(refused=frozenset({"t-2"}))
    summary = await _runner(tmp_path, sweep, setup=_Setup(), loader=loader).run()

    assert summary.failed == 1 and summary.outcomes[1].trace_dir is None
    assert sweep.ran_on == [1, 1]
    assert sweep.log == ["device 1 booted", "device 1 closed"]
    assert _earlier_tasks(summary) == [0, 1]


async def test_a_single_task_outside_a_sweep_keeps_nothing(tmp_path: Path) -> None:
    sweep = _Sweep()
    runner = _runner(tmp_path, sweep, setup=_Setup())

    await runner.run_task("t-1")
    await runner.run_task("t-2")

    assert sweep.log == [
        "device 1 booted",
        "device 1 closed",
        "device 2 booted",
        "device 2 closed",
    ]


async def test_an_environment_with_nothing_to_keep_is_left_alone() -> None:
    reuse = EnvironmentReuse()

    assert await reuse.before_task(None, [_Setup()]) == 0
    await reuse.after_task()
    await reuse.close()
