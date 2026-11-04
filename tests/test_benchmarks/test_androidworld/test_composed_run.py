"""The AndroidWorld benchmark composed with an Android run configuration, as ``run`` and ``bench`` compose it.

``docs/architecture_design.md`` §4.10.1, §4.2. Through the real
``ConfigComposer`` and one ``AgentRuntime.prepare()`` on the freshly composed
stack, with a fake AndroidWorld installation and nothing launched:

- the run configuration writes no AVD and no app table: both come from the
  benchmark, merged into the run's Android block;
- a task whose goal is made on the device at setup (a MiniWoB++ task) is
  known to have its text completed by the Android capability's setup on the
  first ``prepare()``, before the setup component is bound; one whose goal is
  the listed one is not;
- AndroidWorld's live verifier, listed in the benchmark block, is bound to the
  Android capability beside the setup component, and is no part of the
  configuration the run records.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectragent.benchmarks.androidworld.benchmark import AndroidWorldBenchmark
from spectragent.benchmarks.androidworld.service import PINNED_COMMIT
from spectragent.benchmarks.androidworld.setup import AndroidWorldSetup
from spectragent.benchmarks.androidworld.verifier import AndroidWorldVerifier
from spectragent.core.capabilities.android.actions import AndroidOpenAppAction
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.composer import ComposedRun, ConfigComposer
from spectragent.core.runtime import AgentRuntime
from tests._helpers.android_device import fake_service_apk
from tests._helpers.benchmark_data import ANDROIDWORLD_APPS, androidworld_data, androidworld_task
from tests._helpers.fake_androidworld import fake_installation

MINIWOB = "click-button/42/0"
BRIGHTNESS = "SystemBrightnessMax/42/0"


@pytest.fixture
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ComposedRun:
    # The emulator's gRPC token was measured on macOS only (``local_emulator``).
    monkeypatch.setattr(sys, "platform", "darwin")
    data = androidworld_data(
        tmp_path / "data",
        [
            androidworld_task(params={"max_or_min": "max"}),
            androidworld_task(
                "click-button",
                family="miniwob",
                goal="Follow the instructions shown on the top of the screen.",
                goal_at_initialize="made",
            ),
        ],
    )
    root: dict[str, JsonValue] = {
        "agent": [
            {
                "agent_id": "m3a",
                # AndroidWorld's settings, which M3A refuses to run without.
                "agent_loop": {
                    "loop_id": "react",
                    "max_steps": 120,
                    "max_actions_per_step": 2,
                    "max_consecutive_parse_errors": None,
                },
                "llm": {"model_type": "openai", "model": "m", "provider": {"name": "direct"}},
                "memory": {"memory_id": "sliding_window", "window_size": 1},
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
        "benchmark": [
            {
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
        ],
    }
    return ConfigComposer.for_config(root).compose(root)


def _prepared(run: ComposedRun, task_id: str) -> AgentRuntime:
    """One ``prepare()`` for *task_id*, on the stack as it was composed."""
    benchmark = run.single("benchmark")
    assert isinstance(benchmark, AndroidWorldBenchmark)
    context = benchmark.task_loader.get_task_context(task_id)
    runtime = AgentRuntime.for_agent(run, run.ids("agent")[0])
    runtime.prepare(task_id=task_id, env_context=context.env, loop_inputs=context.loop)
    return runtime


def test_the_benchmark_gives_the_avd_and_the_app_table(run: ComposedRun) -> None:
    capability = run.single("capability")
    assert isinstance(capability, AndroidCapability)
    assert capability.backend.avd == "AndroidWorldAvd"  # type: ignore[attr-defined]
    (open_app,) = [a for a in capability.actions if isinstance(a, AndroidOpenAppAction)]
    assert open_app.apps == ANDROIDWORLD_APPS


def test_a_goal_made_on_the_device_is_known_on_the_first_prepare(run: ComposedRun) -> None:
    """The capability answers for the component about to be bound: nothing is bound yet."""
    capability = run.single("capability")
    assert isinstance(capability, AndroidCapability) and capability._setup is None

    runtime = _prepared(run, MINIWOB)

    assert runtime._task_text_setter == "android"
    assert isinstance(capability._setup, AndroidWorldSetup), "bound last"


def test_a_listed_goal_is_the_tasks_text(run: ComposedRun) -> None:
    assert _prepared(run, BRIGHTNESS)._task_text_setter is None


def test_the_live_verifier_is_bound_beside_the_setup_and_not_recorded(run: ComposedRun) -> None:
    capability = run.single("capability")
    assert isinstance(capability, AndroidCapability)

    _prepared(run, BRIGHTNESS)

    (verifier,) = capability._live_verifiers
    assert isinstance(verifier, AndroidWorldVerifier)
    assert verifier.identity().version == PINNED_COMMIT
    recorded = run.run_configuration()
    assert recorded is not None
    (block,) = recorded.model_dump()["benchmark"]
    assert block["verifiers"] is None, "a verifier never changes the run, so it is not recorded"
