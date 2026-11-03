"""The AndroidWorld task loader reads a task list strictly and leaves out what it cannot score.

The list is what ``androidworld_service/task_list.py`` writes in AndroidWorld's
own venv (``docs/architecture_design.md`` §11.2); its record is the Android
task's ``oracle_task``, which the ``androidworld`` oracle accepts unchanged.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue

from spectragent import PACKAGE_ROOT
from spectragent.core.capabilities.android.oracle.androidworld import (
    PINNED_COMMIT,
    AndroidWorldOracle,
)
from spectragent.core.capabilities.android.oracle.androidworld_tasks import (
    TASK_LIST_FORMAT,
    UNREPRODUCIBLE,
    instance_seed,
)
from spectragent.core.data_loader._common import BenchmarkDataError
from spectragent.core.data_loader.task import AndroidWorldTaskLoader
from spectragent.core.data_loader.task._registrar import TaskLoaderRegistrar
from spectragent.core.loops import ReactLoop
from spectragent.schemas.task import TaskContext
from tests._helpers.fake_androidworld import fake_installation

TASK_LIST_WRITER = (
    PACKAGE_ROOT / "core/capabilities/android/oracle/androidworld_service/task_list.py"
)


def _record(
    task: str = "SystemBrightnessMax", family: str = "android", **changes: JsonValue
) -> dict[str, JsonValue]:
    record: dict[str, JsonValue] = {
        "commit": PINNED_COMMIT,
        "family": family,
        "task": task,
        "index": 0,
        "suite_seed": 42,
        "params": {"seed": instance_seed(42, task, 0)},
        "goal": f"Do {task}.",
        "goal_at_initialize": "unchanged",
        "complexity": 1.2,
        "start_on_home_screen": True,
    }
    record.update(changes)
    return record


def _data_dir(tmp_path: Path, listing: object, *, split: str = "All") -> Path:
    data = tmp_path / "androidworld"
    data.mkdir(exist_ok=True)
    (data / f"{split}.json").write_text(
        listing if isinstance(listing, str) else json.dumps(listing)
    )
    (data / "data_info.json").write_text(json.dumps({"split": {split: f"{split}.json"}}))
    return data


def _loader(
    tmp_path: Path, *records: dict[str, JsonValue], subset: str = "All"
) -> AndroidWorldTaskLoader:
    data = _data_dir(tmp_path, {"format": TASK_LIST_FORMAT, "tasks": list(records)}, split=subset)
    return AndroidWorldTaskLoader(data_dir=str(data), subset=subset)


def test_it_registers_as_the_androidworld_benchmark() -> None:
    assert TaskLoaderRegistrar.get("androidworld") is AndroidWorldTaskLoader


def test_each_listed_task_is_an_android_task_whose_record_is_its_oracle_task(
    tmp_path: Path,
) -> None:
    record = _record()
    loader = _loader(tmp_path, record)

    (task_id,) = loader.task_ids()
    task = loader[task_id]
    assert task_id == "SystemBrightnessMax/42/0"
    assert (task.benchmark, task.env_type, task.task_text) == (
        "androidworld",
        "android",
        "Do SystemBrightnessMax.",
    )
    assert task.tags == ["android"] and task.metadata == {"complexity": 1.2}
    assert loader.get_task_context(task_id).env == {"oracle_task": record}


@pytest.mark.parametrize(
    ("complexity", "budget"),
    # The pinned commit's range runs from 1.0 (10 steps) to OsmAndTrack's 12.0
    # (120); 0.15 is not on it and shows the truncation (1.5 steps is 1).
    [(1.0, 10), (1.2, 12), (2.2, 22), (7.8, 78), (12.0, 120), (0.15, 1)],
)
def test_each_task_hands_the_loop_the_steps_androidworld_gives_it(
    tmp_path: Path, complexity: float, budget: int
) -> None:
    """``int(10 * complexity)``, AndroidWorld's ``_allocate_step_budget``, as ReAct's Loop input."""
    loader = _loader(tmp_path, _record(complexity=complexity))

    (task_id,) = loader.task_ids()
    context = loader.get_task_context(task_id)
    assert budget == int(10 * complexity)
    assert context.loop == {"max_steps": budget}
    ReactLoop().check_loop_inputs(context.loop)
    assert ReactLoop(max_steps=200).step_budget(context.loop) == budget


def test_an_unknown_task_has_no_step_budget(tmp_path: Path) -> None:
    assert _loader(tmp_path, _record()).get_task_context("NoSuchTask/42/0") == TaskContext()


def test_information_retrieval_tasks_are_kept_and_the_oracle_accepts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scored on the run's answer, which the scoring point hands the oracle (§4.2)."""
    asking = _record("SimpleCalendarLocationOfEvent", "information_retrieval")
    loader = _loader(tmp_path, _record(), asking)

    task_id = "SimpleCalendarLocationOfEvent/42/0"
    assert loader.task_ids() == ["SystemBrightnessMax/42/0", task_id]
    assert loader.skipped == {}
    assert loader[task_id].tags == ["information_retrieval"]
    oracle_task = loader.get_task_context(task_id).env["oracle_task"]
    assert oracle_task == asking
    installation = fake_installation(tmp_path / "aw", monkeypatch)
    AndroidWorldOracle(root=str(installation)).check_setup(oracle_task)


def test_tasks_the_service_cannot_build_again_are_left_out_with_the_oracles_reason(
    tmp_path: Path,
) -> None:
    loader = _loader(tmp_path, _record(), _record("MarkorCreateFolder"))

    assert loader.task_ids() == ["SystemBrightnessMax/42/0"]
    assert loader.skipped["MarkorCreateFolder/42/0"] == UNREPRODUCIBLE["MarkorCreateFolder"]


def test_skipped_tasks_are_counted_in_one_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="spectragent.core.data_loader.task.androidworld")
    _loader(
        tmp_path,
        _record(),
        _record("SimpleCalendarLocationOfEvent", "information_retrieval"),
        _record("MarkorCreateFolder"),
    )

    assert [record.getMessage() for record in caplog.records] == [
        "androidworld: left out 1 of the 3 listed tasks; the reasons are in the loader's skipped"
    ]


def test_a_list_with_nothing_to_run_is_refused_naming_why(tmp_path: Path) -> None:
    with pytest.raises(BenchmarkDataError, match="no task this loader runs: .*host's clock"):
        _loader(tmp_path, _record("MarkorCreateFolder"))


@pytest.mark.parametrize(
    ("listing", "message"),
    [
        ("not json", "is not an AndroidWorld task list"),
        ({"format": "androidworld-tasks-0", "tasks": []}, "format"),
        ({"format": TASK_LIST_FORMAT, "tasks": [_record(goal=None)]}, "goal"),
        ({"format": TASK_LIST_FORMAT, "tasks": [{**_record(), "extra": 1}]}, "extra"),
        ({"format": TASK_LIST_FORMAT, "tasks": [_record(params={"seed": 7})]}, "params.seed is 7"),
        ({"format": TASK_LIST_FORMAT, "tasks": [_record(), _record()]}, "more than once"),
        ({"tasks": [_record()]}, "format"),
    ],
)
def test_a_list_that_breaks_the_format_is_refused_and_nothing_is_published(
    tmp_path: Path, listing: object, message: str
) -> None:
    data = _data_dir(tmp_path, listing)

    with pytest.raises(BenchmarkDataError, match=message):
        AndroidWorldTaskLoader(data_dir=str(data))


def test_the_subset_picks_the_split_file(tmp_path: Path) -> None:
    _data_dir(tmp_path, {"format": TASK_LIST_FORMAT, "tasks": [_record()]})
    data = _data_dir(
        tmp_path,
        {"format": TASK_LIST_FORMAT, "tasks": [_record("click-button", "miniwob")]},
        split="miniwob",
    )
    (data / "data_info.json").write_text(
        json.dumps({"split": {"All": "All.json", "miniwob": "miniwob.json"}})
    )

    loader = AndroidWorldTaskLoader(data_dir=str(data), subset="miniwob")
    assert loader.task_ids() == ["click-button/42/0"]


def test_the_oracle_accepts_the_task_the_loader_hands_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = _loader(tmp_path, _record())
    oracle = AndroidWorldOracle(root=str(fake_installation(tmp_path / "aw", monkeypatch)))

    oracle.check_setup(loader.get_task_context("SystemBrightnessMax/42/0").env["oracle_task"])


def test_the_writer_and_the_reader_name_one_format() -> None:
    assert f'FORMAT = "{TASK_LIST_FORMAT}"' in TASK_LIST_WRITER.read_text()


def test_reading_a_list_imports_no_android_implementation(tmp_path: Path) -> None:
    """Every run imports this loader with the framework; a run that names no Android must not
    pay for the capability, its bridge or the oracle that starts processes (§4.2.1)."""
    data = _data_dir(tmp_path, {"format": TASK_LIST_FORMAT, "tasks": [_record()]})
    probe = (
        "import json, sys\n"
        "from spectragent.core.data_loader.task import AndroidWorldTaskLoader\n"
        f"assert len(AndroidWorldTaskLoader(data_dir={str(data)!r})) == 1\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    loaded = set(
        json.loads(
            subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True, check=True
            ).stdout
        )
    )
    android = "spectragent.core.capabilities.android"
    assert f"{android}.oracle.androidworld_tasks" in loaded, "the probe read a list"
    assert not {f"{android}.capability", f"{android}.oracle.androidworld"} & loaded
